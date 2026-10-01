"""Trello: клиент REST API, оформление карточки и выполнение задач outbox.

Карточка создаётся на первом сообщении клиента. Каждое сообщение переписки — отдельный комментарий,
анкета — в описании карточки и обновляется по мере ответов. После «Взял в работу» карточка едет по спискам
вслед за этапами, которые отмечает менеджер (замер, договор, отказ).
"""

import asyncio
import logging
import mimetypes
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.db import (
    CARD_ATTACH,
    CARD_COMMENT,
    CARD_CREATE,
    CARD_DELETE,
    CARD_STAGE,
    CARD_TAKE,
    CARD_TRANSCRIPT,
    CARD_UPDATE,
    Database,
    Lead,
    Message,
    OutboxTask,
)
from app.services.tgfiles import FileTooLarge
from app.stages import CONTRACT, MEASURE, NO_ANSWER, REFUSED, THINKING, history_lines, stage_text

log = logging.getLogger(__name__)

API = "https://api.trello.com/1"
COMMENT_LIMIT = 16384  # лимит Trello на комментарий

# Метки, которые бот создаёт на доске: ключ → (название, цвет).
LABELS = {
    "qualified": ("квалифицирован", "green"),
    "abandoned": ("не завершил анкету", "orange"),
    "cancelled": ("закрыта клиентом", "black"),
    "night": ("ночной", "purple"),
}

MODEL_NAMES = {"script": "скрипт (без LLM)"}

ATTACH_GIVE_UP = 3  # файл не прикрепился за столько попыток — пишем об этом комментарий и идём дальше
FILE_NAMES = {"voice": "голосовое", "photo": "фото", "document": "файл", "video_note": "видео"}
DEFAULT_EXT = {"voice": ".ogg", "photo": ".jpg", "video_note": ".mp4"}

FetchFile = Callable[[str], Awaitable[tuple[bytes, str]]]

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<])")


def md(text: str | None) -> str:
    """Экранировать Markdown в чужом тексте (клиент, модель, менеджер): иначе в карточке можно спрятать
    ссылку под безобидным текстом [Оплатить](http://…) или вставить картинку-трекер ![](…)."""
    return _MD_SPECIAL.sub(r"\\\1", text or "")


class TrelloError(Exception):
    pass


class TrelloClient:
    def __init__(self, api_key: str, token: str, http: httpx.AsyncClient | None = None):
        self._auth = {"key": api_key, "token": token}
        self._http = http or httpx.AsyncClient(base_url=API, timeout=20)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, path: str, json: dict[str, Any] | None = None, *,
        files: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
    ) -> Any:
        try:
            resp = await self._http.request(method, path, params=self._auth, json=json, files=files, data=data)
        except httpx.HTTPError as e:
            # В тексте исключения httpx может быть URL с ключами — наружу только тип ошибки.
            raise TrelloError(f"{method} {path}: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise TrelloError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def lists(self, board_id: str) -> list[dict]:
        return await self._call("GET", f"/boards/{board_id}/lists")

    async def create_list(self, board_id: str, name: str) -> dict:
        return await self._call("POST", f"/boards/{board_id}/lists", {"name": name, "pos": "bottom"})

    async def labels(self, board_id: str) -> list[dict]:
        return await self._call("GET", f"/boards/{board_id}/labels")

    async def create_label(self, board_id: str, name: str, color: str) -> dict:
        return await self._call("POST", f"/boards/{board_id}/labels", {"name": name, "color": color})

    async def create_card(self, list_id: str, name: str, desc: str, label_ids: list[str]) -> dict:
        return await self._call(
            "POST", "/cards", {"idList": list_id, "name": name, "desc": desc, "idLabels": ",".join(label_ids)}
        )

    async def card(self, card_id: str) -> dict:
        return await self._call("GET", f"/cards/{card_id}")

    async def update_card(self, card_id: str, **fields: Any) -> dict:
        return await self._call("PUT", f"/cards/{card_id}", fields)

    async def delete_card(self, card_id: str) -> None:
        try:
            await self._call("DELETE", f"/cards/{card_id}")
        except TrelloError as e:
            if "HTTP 404" not in str(e):
                raise  # 404 — карточки уже нет (удалили вручную или повтор задачи): это успех

    async def add_comment(self, card_id: str, text: str) -> dict:
        return await self._call("POST", f"/cards/{card_id}/actions/comments", {"text": text[:COMMENT_LIMIT]})

    async def add_attachment(self, card_id: str, filename: str, content: bytes, mime: str) -> dict:
        return await self._call(
            "POST", f"/cards/{card_id}/attachments",
            files={"file": (filename, content, mime)}, data={"name": filename, "mimeType": mime},
        )


# --- оформление ---


def _local(ts: str, zone: ZoneInfo) -> datetime:
    return datetime.fromisoformat(ts).astimezone(zone)


def _area(lead: Lead) -> str | None:
    if lead.area_m2 is not None:
        return f"~{lead.area_m2:g} м²"
    return lead.area_text


HOT_ICONS = {"горячий": "🔥", "тёплый": "🌤", "холодный": "❄️"}


def card_name(lead: Lead) -> str:
    phone = lead.phone if lead.phone and lead.phone.startswith("+") else None
    parts = [lead.name or "Клиент", lead.object, _area(lead), phone]
    icon = f"{HOT_ICONS[lead.hotness]} " if lead.hotness in HOT_ICONS else ""
    return f"{icon}№{lead.id} · " + " · ".join(p for p in parts if p)


STATUS_NAMES = {
    "new": "заполняет анкету", "qualified": "анкета заполнена", "abandoned": "не завершил анкету",
    "cancelled": "закрыта клиентом — начал новую заявку",
}


def card_desc(lead: Lead, zone: ZoneInfo, previous: Sequence[Lead] = ()) -> str:
    area = lead.area_text
    if lead.area_m2 is not None and lead.area_text and f"{lead.area_m2:g}" not in lead.area_text:
        area = f"{lead.area_text} (~{lead.area_m2:g} м²)"
    dash = "—"
    contact = md(lead.name) or "без имени"
    if lead.username:
        contact += f", [@{lead.username}](https://t.me/{lead.username})"
    created = _local(lead.created_at, zone)
    lines = [
        f"**Заявка №{lead.id}**" + (" · 🌙 ночная" if lead.is_night else ""),
        f"Статус: {STATUS_NAMES.get(lead.status, lead.status)}",
        *stage_lines(lead, zone),
        "",
        f"**Объект:** {md(lead.object) or dash}",
        f"**Площадь:** {md(area) or dash}",
        f"**Потолок:** {md(lead.ceiling_type) or dash}",
        f"**Телефон:** {md(lead.phone) or dash}",
        f"**Замер:** {md(lead.measure_time) or dash}",
        "",
        *summary_lines(lead),
        *previous_lines(previous, zone),
        f"**Клиент в Telegram:** {contact} (ID {lead.tg_user_id})",
        *([f"**Источник:** {md(lead.source)}"] if lead.source else []),
        f"**Создана:** {created:%d.%m.%Y %H:%M} ({zone.key})",
        "",
        "Переписка — в комментариях.",
    ]
    return "\n".join(lines)


def previous_lines(previous: Sequence[Lead], zone: ZoneInfo) -> list[str]:
    """Прошлые заявки этого клиента."""
    if not previous:
        return []
    return ["**Уже обращался:**", *(f"- {md(line)}" for line in history_lines(previous, zone)), ""]


def stage_lines(lead: Lead, zone: ZoneInfo) -> list[str]:
    """Этап взятой заявки и кто её ведёт."""
    if not lead.taken_at:
        return []
    stage = md(stage_text(lead.stage, lead.measure_at, lead.refuse_reason, zone))
    lines = [f"**Этап:** {stage} · ведёт {md(lead.taken_by_name)}"]
    if lead.rating:
        lines.append(f"**Оценка замера:** {'⭐' * lead.rating} {lead.rating} из 5")
    return lines


def summary_lines(lead: Lead) -> list[str]:
    if not lead.summary:
        return []
    hot = f"**Оценка:** {HOT_ICONS.get(lead.hotness, '')} {lead.hotness}" if lead.hotness else ""
    if hot and lead.hotness_reason:
        hot += f" — {md(lead.hotness_reason)}"
    lines = [hot] if hot else []
    return [*lines, f"**Резюме:** {md(lead.summary)}", f"_— резюме: модель {lead.summary_model}_", ""]


def stt_signature(msg: Message) -> str:
    """Какая модель расшифровала голосовое — как подпись модели под ответами бота, видно только в Trello."""
    return f"\n\n_— расшифровка: {md(msg.model)}_" if msg.model else ""


def comment_text(msg: Message, zone: ZoneInfo) -> str:
    when = f"{_local(msg.created_at, zone):%d.%m %H:%M}"
    if msg.direction == "out" and msg.kind == "manager":
        attached = " (во вложениях)" if msg.file_id else ""
        return f"👔 **Менеджер {md(msg.model)}** · {when}\n\n{md(msg.text)}{attached}"
    if msg.direction == "out":
        model = MODEL_NAMES.get(msg.model or "script", msg.model)
        return f"🤖 **Бот** · {when}\n\n{md(msg.text)}\n\n_— модель: {model}_"
    text = md(msg.text)
    body = {
        "button": f"Нажал кнопку: **{text}**",
        "contact": f"📱 Поделился номером: {text}",
        "voice": (f"🎤 Голосовое:\n\n> {text}{stt_signature(msg)}" if msg.text
                  else "🎤 Голосовое (расшифровка будет ниже)"),
        "photo": "📷 Фото" + (f": {text}" if msg.text else "") + " (во вложениях)",
        "document": "📎 Файл" + (f": {text}" if msg.text else "") + " (во вложениях)",
        "video_note": "📹 Видеосообщение (во вложениях)",
    }.get(msg.kind, text)
    return f"👤 **Клиент** · {when}\n\n{body}"


# --- синхронизация ---


@dataclass
class Board:
    list_new: str
    list_in_work: str
    labels: dict[str, str]  # ключ из LABELS → id метки
    stage_lists: dict[str | None, str] = field(default_factory=dict)  # этап → id списка


class TrelloSync:
    """Выполняет задачи outbox: создать карточку, обновить анкету, добавить комментарий."""

    def __init__(
        self, db: Database, client: TrelloClient, board_id: str, zone: ZoneInfo, list_new: str, list_in_work: str,
        fetch_file: FetchFile | None = None, *, stage_lists: tuple[str, str, str] = ("Замер", "Договор", "Отказ"),
    ):
        self.db, self.client, self.board_id, self.zone = db, client, board_id, zone
        # Новые, в работе, замер, договор, отказ.
        self.list_names = (list_new, list_in_work, *stage_lists)
        self.fetch_file = fetch_file  # скачивание файлов из Telegram для вложений
        self._board: Board | None = None
        self._board_lock = asyncio.Lock()  # задачи идут параллельно — доску готовим один раз

    @property
    def handlers(self):
        return {
            CARD_CREATE: self.create_card,
            CARD_UPDATE: self.update_card,
            CARD_COMMENT: self.add_comment,
            CARD_TAKE: self.take_card,
            CARD_ATTACH: self.attach_file,
            CARD_TRANSCRIPT: self.add_transcript,
            CARD_DELETE: self.delete_card,
            CARD_STAGE: self.stage_card,
        }

    async def board(self) -> Board:
        """Найти списки и метки на доске, недостающие создать. Результат кешируется."""
        async with self._board_lock:
            if self._board is None:
                self._board = await self._prepare_board()
            return self._board

    async def _prepare_board(self) -> Board:
        lists = {lst["name"]: lst["id"] for lst in await self.client.lists(self.board_id) if not lst.get("closed")}
        for name in self.list_names:
            if name not in lists:
                log.info("Trello: создаю список «%s»", name)
                lists[name] = (await self.client.create_list(self.board_id, name))["id"]
        existing = {lbl["name"]: lbl["id"] for lbl in await self.client.labels(self.board_id) if lbl.get("name")}
        labels = {}
        for key, (name, color) in LABELS.items():
            if name not in existing:
                log.info("Trello: создаю метку «%s»", name)
                existing[name] = (await self.client.create_label(self.board_id, name, color))["id"]
            labels[key] = existing[name]
        new, in_work, measure, won, lost = (lists[name] for name in self.list_names)
        stage_lists = {None: in_work, NO_ANSWER: in_work, MEASURE: measure, THINKING: measure, CONTRACT: won,
                       REFUSED: lost}
        return Board(new, in_work, labels, stage_lists)

    def label_ids(self, lead: Lead, board: Board) -> list[str]:
        keys = []
        if lead.status in ("qualified", "abandoned", "cancelled"):
            keys.append(lead.status)
        if lead.is_night:
            keys.append("night")
        return [board.labels[k] for k in keys]

    async def _lead(self, task: OutboxTask) -> Lead:
        lead = await self.db.get_lead(task.lead_id)
        if lead is None:
            raise TrelloError(f"lead {task.lead_id} not found")
        return lead

    async def _desc(self, lead: Lead) -> str:
        return card_desc(lead, self.zone, await self.db.previous_leads(lead))

    async def _card_id(self, lead: Lead) -> str:
        if not lead.trello_card_id:
            # Сюда не попадаем при нормальном порядке: создание карточки идёт первой задачей лида.
            raise TrelloError(f"lead {lead.id} has no card yet")
        return lead.trello_card_id

    async def create_card(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        if lead.trello_card_id or lead.status == "deleted":
            return  # уже создана (повтор задачи после сбоя) или клиент удалил заявку
        board = await self.board()
        card = await self.client.create_card(
            board.list_new, card_name(lead), await self._desc(lead), self.label_ids(lead, board)
        )
        await self.db.update_lead(lead.id, trello_card_id=card["id"], trello_card_url=card.get("shortUrl"))
        log.info("Trello: карточка для лида %s создана: %s", lead.id, card.get("shortUrl"))

    async def delete_card(self, task: OutboxTask) -> None:
        """Клиент удалил заявку: карточка удаляется вместе с перепиской и вложениями."""
        lead = await self._lead(task)
        if lead.trello_card_id:
            await self.client.delete_card(lead.trello_card_id)
            await self.db.update_lead(lead.id, trello_card_id=None, trello_card_url=None)

    async def update_card(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        board = await self.board()
        card_id = await self._card_id(lead)
        # Метки, поставленные менеджером вручную, сохраняем — управляем только своими.
        ours = set(board.labels.values())
        manual = [i for i in (await self.client.card(card_id)).get("idLabels", []) if i not in ours]
        await self.client.update_card(
            card_id,
            name=card_name(lead),
            desc=await self._desc(lead),
            idLabels=",".join(manual + self.label_ids(lead, board)),
        )

    async def take_card(self, task: OutboxTask) -> None:
        """Менеджер нажал «Взял в работу»: карточка переезжает в список «В работе»."""
        lead = await self._lead(task)
        board = await self.board()
        card_id = await self._card_id(lead)
        await self.client.update_card(card_id, idList=board.list_in_work, desc=await self._desc(lead))
        await self.client.add_comment(card_id, f"✅ Взял в работу: **{md(task.payload['by'])}**")

    async def stage_card(self, task: OutboxTask) -> None:
        """Менеджер отметил этап: карточка — в список этапа, в переписке — кто и что отметил."""
        lead = await self._lead(task)
        board = await self.board()
        card_id = await self._card_id(lead)
        p = task.payload
        stage = p["stage"]
        text = stage_text(stage, p.get("measure_at"), p.get("reason"), self.zone) if stage else "↩️ Снова в работе"
        await self.client.update_card(card_id, idList=board.stage_lists[stage])
        await self.client.add_comment(card_id, f"{md(text)} — **{md(p['by'])}**")

    async def _message(self, task: OutboxTask) -> Message:
        msg = await self.db.get_message(task.payload["message_id"])
        if msg is None:
            raise TrelloError(f"message {task.payload['message_id']} not found")
        return msg

    async def add_comment(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        msg = await self._message(task)
        await self.client.add_comment(await self._card_id(lead), comment_text(msg, self.zone))

    async def attach_file(self, task: OutboxTask) -> None:
        """Приложить к карточке файл из Telegram. Не вышло за несколько попыток — комментарий и дальше:
        одна неудачная загрузка не должна задерживать остальную переписку."""
        lead = await self._lead(task)
        msg = await self._message(task)
        card_id = await self._card_id(lead)
        if self.fetch_file is None or not msg.file_id:
            return
        base = FILE_NAMES.get(msg.kind, "файл")
        try:
            content, tg_path = await self.fetch_file(msg.file_id)
            ext = PurePosixPath(tg_path).suffix or DEFAULT_EXT.get(msg.kind, "")
            if ext == ".oga":
                ext = ".ogg"  # так файл открывается плеером в браузере
            mime = mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
            await self.client.add_attachment(card_id, f"{base}_{msg.id}{ext}", content, mime)
        except Exception as e:
            if task.attempts + 1 < ATTACH_GIVE_UP and not isinstance(e, FileTooLarge):
                raise
            log.error("Не удалось приложить %s к карточке лида %s: %s", base, lead.id, e)
            when = f"{_local(msg.created_at, self.zone):%d.%m %H:%M}"
            note = f"⚠️ Не удалось приложить {base} от {when} — смотрите в чате Telegram"
            await self.client.add_comment(card_id, note)

    async def add_transcript(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        msg = await self._message(task)
        when = f"{_local(msg.created_at, self.zone):%d.%m %H:%M}"
        if task.payload.get("failed"):
            text = f"🎤 Голосовое от {when}: расшифровать не удалось — прослушайте вложение"
        else:
            text = f"🎤 Расшифровка голосового от {when}:\n\n> {md(msg.text)}{stt_signature(msg)}"
        await self.client.add_comment(await self._card_id(lead), text)
