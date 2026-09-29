"""LLM-ассистент: шаг диалога с клиентом и резюме лида для менеджера.

Ответ модели проверяется здесь: невалидный ответ — это ошибка модели, и маршрутизатор
переходит к следующей. Поля анкеты из ответа нормализуются так же, как ответы по скрипту.
"""

import difflib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from functools import partial
from typing import Any

from app.bot import prompts, texts
from app.bot.facts import CeilingVocab, StudioFacts, default_facts, is_adjective, rubles, stem
from app.db import Lead, Message
from app.parsing import AREA_MAX, AREA_MIN, clip, normalize_phone, parse_area, replace_phones
from app.services.llm import LLMRouter

HISTORY_LIMIT = 20       # сообщений переписки в контексте шага диалога
MESSAGE_MAX = 1000       # символов одного сообщения из истории в контексте LLM
NEW_MESSAGE_MAX = 2000   # символов нового сообщения клиента (Telegram пускает до 4096 — это токены и деньги)
SUMMARY_LIMIT = 8000     # символов переписки в контексте резюме
REPLY_LIMIT = 1500
HOTNESS = {"горячий": "горячий", "тёплый": "тёплый", "теплый": "тёплый", "холодный": "холодный"}

FIELD_ORDER = list(prompts.FIELDS)  # object, area, ceiling_type, phone, measure_time

# Телефоны клиентов LLM-провайдерам не передаём: бот находит номер сам, модели достаётся «[телефон]».
PHONE_MASK = "[телефон]"
def mask_phones(text: str) -> tuple[str, list[str]]:
    """Текст без номеров телефонов и найденные номера (+7XXXXXXXXXX)."""
    found: list[str] = []

    def replace(phone: str) -> str:
        found.append(phone)
        return PHONE_MASK

    return replace_phones(text, replace), found


def known_fields(lead: Lead, *, mask_phone: bool = False) -> dict[str, str | None]:
    area = lead.area_text or (f"{lead.area_m2:g} м²" if lead.area_m2 is not None else None)
    phone = lead.phone
    if mask_phone and phone and phone.startswith("+"):
        phone = "указан"
    return {
        "object": lead.object,
        "area": area,
        "ceiling_type": lead.ceiling_type,
        "phone": phone,
        "measure_time": lead.measure_time,
    }


def missing_fields(lead: Lead) -> list[str]:
    known = known_fields(lead)
    return [f for f in FIELD_ORDER if not known[f]]


def _line(m: Message) -> str:
    if m.kind == "voice":
        return m.text or "[голосовое сообщение]"
    if m.kind == "button":
        return m.text or ""
    if m.kind in ("photo", "document", "video_note"):
        return f"[{m.kind}] {m.text or ''}".strip()
    return m.text or ""


def history_messages(history: list[Message]) -> list[dict[str, str]]:
    return [
        {"role": "user" if m.direction == "in" else "assistant", "content": clip(mask_phones(_line(m))[0], MESSAGE_MAX)}
        for m in history[-HISTORY_LIMIT:]
        if _line(m)
    ]


def _str(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "нет данных"):
        return None
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return n if AREA_MIN <= n <= AREA_MAX else None


@dataclass
class Turn:
    reply: str
    updates: dict[str, Any]  # поля лида для db.update_lead
    asks: str | None = None  # про какое поле вопрос в конце reply (None — ничего не спрашивает)
    model: str = ""


# Тип помещения (в отличие от комнаты): по нему видно, что клиент меняет сам объект, а не уточняет комнату.
_OBJECT_TYPE = re.compile(
    r"\b(квартир\w*|дом\w{0,2}|частн\w*|коттедж\w*|таунхаус\w*|дач\w*|офис\w*|коммерч\w*|магазин\w*|"
    r"кафе|ресторан\w*|салон\w*|склад\w*|студи[яюи]|однушк\w*|двушк\w*|тр[её]шк\w*|\w+комнатн\w*)\b",
    re.IGNORECASE,
)


def merge_object(current: str | None, new: str) -> str:
    """Что записать в «помещение», если оно уже известно. Кнопка «Квартира», потом «в спальню» —
    «Квартира, спальня»: комната уточняет тип, а не заменяет его. Новый тип («нет, это дом») — исправление."""
    if not current or _OBJECT_TYPE.search(new):
        return new
    kind = current.split(",")[0].strip()
    return f"{kind}, {new}" if _OBJECT_TYPE.search(kind) else new


# Номер вопроса анкеты («3/4. Какой потолок…») — это разметка бота; модель иногда копирует её в свой ответ.
_QUESTION_NUMBER = re.compile(r"(^|\s)[1-4]\s*/\s*4\.?\s+(?=[А-ЯЁA-Z])")
# Слова, которые в перечислении видов потолков не являются видом.
_NOT_A_KIND = {
    "может", "быть", "или", "и", "а", "еще", "также", "например", "есть", "либо", "потолок", "потолки", "потолка",
    "натяжной", "натяжные", "натяжного", "какой", "какие", "какую", "другой", "другие", "другое", "любой", "иной",
    "нужный", "подходящий", "интересует", "нужен", "вам", "вас", "же", "то", "это", "вот", "тоже", "пока",
}


def _known(word_stem: str, stems: frozenset[str]) -> bool:
    """Основа совпадает или одна продолжает другую («парящ» — «парящ»; «светов» — «световы…»)."""
    return any(
        word_stem == s or (min(len(word_stem), len(s)) >= 4 and (word_stem.startswith(s) or s.startswith(word_stem)))
        for s in stems
    )


# Виды потолков, которые бывают в отрасли: если студия их не указала в фактах — модели их не предлагать.
_INDUSTRY_KINDS = ("двухуровнев", "многоуровнев", "трехуровнев", "звездн", "резн", "перфорирован", "зеркальн",
                   "фактурн", "текстурн", "лакирован", "витражн", "ступенчат", "фотопечат")


def _typo_of_kind(word: str, kind_words: frozenset[str]) -> bool:
    """Похоже на искажённое название вида из фактов (не само название): «парижский» ~ «парящий»."""
    return any(difflib.SequenceMatcher(None, word, k).ratio() >= 0.6 for k in kind_words)


def check_ceiling_types(reply: str, vocab: CeilingVocab) -> None:
    """В перечислении видов потолков вид из фактов о студии можно назвать как угодно, а незнакомое слово — брак
    ответа (следующая модель или скрипт), если это искажённое название вида («парижский» вместо «парящий») или
    вид, которого у студии нет («двухуровневый»). Цвета и оценки («белый», «практичный») не трогаем."""
    for sentence in re.split(r"[.!?\n]+", reply.lower().replace("ё", "е")):
        words = re.findall(r"[а-я]+", sentence)
        kindish = [w for w in words
                   if is_adjective(w) and (_known(stem(w), vocab.core) or _typo_of_kind(w, vocab.words))]
        if len(kindish) < 2:
            continue  # не перечисление видов (слова с опечаткой в названии вида тоже считаем)
        for item in re.split(r"[,;:—–()]|\bили\b|\bи\b", sentence):
            tokens = [w for w in re.findall(r"[а-я]+", item) if w not in _NOT_A_KIND]
            if len(tokens) != 1 or not is_adjective(tokens[0]) or _known(stem(tokens[0]), vocab.known):
                continue
            word = tokens[0]
            if stem(word).startswith(_INDUSTRY_KINDS) or _typo_of_kind(word, vocab.words):
                raise ValueError(f"вид потолка не из фактов о студии: {word}")


def check_amounts(reply: str, allowed: Collection[int]) -> None:
    """Модель может называть только суммы из фактов о студии. Резервная модель сама умножала цену за м² на площадь
    («от 34 000 ₽» за зал) — такая «смета» выглядит как обещание студии, а честно её считают только на замере."""
    if unknown := rubles(reply) - set(allowed):
        raise ValueError(f"сумма не из фактов о студии: {sorted(unknown)}")


def parse_turn(data: dict, allowed: Collection[int] | None = None, types: CeilingVocab | None = None) -> Turn:
    reply = _str(data.get("reply"), REPLY_LIMIT)
    if not reply:
        raise ValueError("пустой reply")
    reply = _QUESTION_NUMBER.sub(r"\1", reply).strip()
    check_amounts(reply, default_facts().allowed_amounts if allowed is None else allowed)
    check_ceiling_types(reply, default_facts().ceiling_types if types is None else types)
    fields = data.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("fields не объект")

    updates: dict[str, Any] = {}
    if obj := _str(fields.get("object"), 120):
        updates["object"] = obj
    area_text = _str(fields.get("area_text"), 120)
    area_m2 = _number(fields.get("area_m2"))
    if area_m2 is None and area_text:
        area_m2 = parse_area(area_text)
    if area_m2 is not None or area_text:
        updates["area_m2"] = area_m2
        updates["area_text"] = area_text or f"{area_m2:g} м²"
    if ct := _str(fields.get("ceiling_type"), 120):
        updates["ceiling_type"] = ct
    if (phone := _str(fields.get("phone"), 40)) and (normalized := normalize_phone(phone)):
        updates["phone"] = normalized
    elif fields.get("phone_refused") is True:
        updates["phone"] = texts.NO_PHONE_VALUE
    if mt := _str(fields.get("measure_time"), 200):
        updates["measure_time"] = mt
    asks = _str(data.get("asks"), 20)
    return Turn(reply, updates, asks if asks in FIELD_ORDER else None)


@dataclass
class Summary:
    summary: str
    hotness: str
    reason: str
    model: str = ""


def parse_summary(data: dict) -> Summary:
    summary = _str(data.get("summary"), 1000)
    hotness = HOTNESS.get((_str(data.get("hotness"), 20) or "").lower())
    if not summary or not hotness:
        raise ValueError("нет summary или hotness")
    return Summary(summary, hotness, _str(data.get("reason"), 300) or "")


def transcript(history: list[Message]) -> str:
    lines = [f"{'Клиент' if m.direction == 'in' else 'Бот'}: {mask_phones(_line(m))[0]}" for m in history if _line(m)]
    text = "\n".join(lines)
    return text[-SUMMARY_LIMIT:]


class LeadAssistant:
    def __init__(self, router: LLMRouter, facts: StudioFacts | None = None):
        self.router = router
        self.facts = facts or default_facts()

    async def dialog_turn(
        self, lead: Lead, history: list[Message], new_text: str | None, *, done: bool, eta: str
    ) -> Turn:
        """Ответ клиенту и поля анкеты из его последнего сообщения. LLMError — ни одна модель не справилась."""
        system = prompts.dialog_system(
            known_fields(lead, mask_phone=True), missing_fields(lead) or ["measure_time"],
            done=done, lead_id=lead.id, eta=eta, facts=self.facts.text,
        )
        messages = [{"role": "system", "content": system}, *history_messages(history)]
        phones: list[str] = []
        if new_text is not None:
            masked, phones = mask_phones(new_text)
            messages.append({"role": "user", "content": clip(masked, NEW_MESSAGE_MAX)})
        messages.append({"role": "system", "content": prompts.FORMAT_REMINDER})
        check = partial(parse_turn, allowed=self.facts.allowed_amounts, types=self.facts.ceiling_types)
        turn, model = await self.router.json(messages, check)
        turn.model = model
        if phones and "phone" not in turn.updates:
            turn.updates["phone"] = phones[0]  # номер нашли сами — модель видела только «[телефон]»
        return turn

    async def summarize(self, lead: Lead, history: list[Message]) -> Summary:
        status = {"qualified": "анкета заполнена", "abandoned": "анкету не закончил"}.get(lead.status, lead.status)
        anketa = json.dumps(known_fields(lead, mask_phone=True), ensure_ascii=False)
        # Тег закрытия в тексте клиента ломается: иначе клиент мог бы «закончить» переписку и дописать свои указания.
        dialog = transcript(history).replace("</переписка>", "</ переписка>")
        user = f"Статус: {status}.\nАнкета: {anketa}\n\n<переписка>\n{dialog}\n</переписка>"
        summary, model = await self.router.json(
            [
                {"role": "system", "content": prompts.SUMMARY_SYSTEM},
                {"role": "user", "content": user},
                {"role": "system", "content": prompts.FORMAT_REMINDER},
            ],
            parse_summary, temperature=0.2, max_tokens=400,
        )
        summary.model = model
        return summary
