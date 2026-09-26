import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.db import CARD_COMMENT, CARD_CREATE, CARD_UPDATE, Database
from app.services.outbox import Outbox
from app.services.trello import LABELS, TrelloClient, TrelloError, TrelloSync, card_name, comment_text
from tests.conftest import USER, Client

ZONE = ZoneInfo("Europe/Moscow")


class FakeTrello(TrelloClient):
    """Доска Trello в памяти. fail[метод] = n — метод упадёт n раз подряд."""

    def __init__(self, lists=("Новые запросы", "В работе"), labels=()):
        self.lists_ = [{"id": f"L{i}", "name": n} for i, n in enumerate(lists)]
        self.labels_ = [{"id": f"B{i}", "name": n} for i, n in enumerate(labels)]
        self.cards: dict[str, dict] = {}
        self.fail: dict[str, int] = {}
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail.get(name):
            self.fail[name] -= 1
            raise TrelloError(f"{name}: HTTP 503")

    async def lists(self, board_id):
        self._maybe_fail("lists")
        return self.lists_

    async def create_list(self, board_id, name):
        self._maybe_fail("create_list")
        self.lists_.append(item := {"id": f"L{len(self.lists_)}", "name": name})
        return item

    async def labels(self, board_id):
        self._maybe_fail("labels")
        return self.labels_

    async def create_label(self, board_id, name, color):
        self._maybe_fail("create_label")
        self.labels_.append(item := {"id": f"B{len(self.labels_)}", "name": name, "color": color})
        return item

    async def create_card(self, list_id, name, desc, label_ids):
        self._maybe_fail("create_card")
        card_id = f"C{len(self.cards) + 1}"
        self.cards[card_id] = {"idList": list_id, "name": name, "desc": desc, "idLabels": label_ids, "comments": []}
        return {"id": card_id, "shortUrl": f"https://trello.com/c/{card_id}"}

    async def card(self, card_id):
        self._maybe_fail("card")
        return {"id": card_id, "idLabels": list(self.cards[card_id]["idLabels"])}

    async def update_card(self, card_id, **fields):
        self._maybe_fail("update_card")
        card = self.cards[card_id]
        if "idLabels" in fields:
            fields["idLabels"] = [i for i in fields["idLabels"].split(",") if i]
        card.update(fields)
        return card

    async def add_comment(self, card_id, text):
        self._maybe_fail("add_comment")
        self.cards[card_id]["comments"].append(text)
        return {"id": "A"}

    async def add_attachment(self, card_id, filename, content, mime):
        self._maybe_fail("add_attachment")
        self.cards[card_id].setdefault("attachments", []).append((filename, content, mime))
        return {"id": "F"}

    def label_names(self, card_id: str) -> set[str]:
        by_id = {lbl["id"]: lbl["name"] for lbl in self.labels_}
        return {by_id[i] for i in self.cards[card_id]["idLabels"]}


def make_sync(db: Database, fake: FakeTrello) -> TrelloSync:
    return TrelloSync(db, fake, "board", ZONE, "Новые запросы", "В работе")


async def complete_dialog(client: Client) -> None:
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("18,5")
    await client.press("ct:matte")
    await client.contact("79001234567")
    await client.text("в субботу")


# --- HTTP-слой ---


@respx.mock
async def test_client_sends_auth_and_json():
    route = respx.post("https://api.trello.com/1/cards").mock(return_value=httpx.Response(200, json={"id": "C1"}))
    client = TrelloClient("KEY", "TOKEN")
    card = await client.create_card("L1", "Имя", "Описание", ["B1", "B2"])
    assert card == {"id": "C1"}
    req = route.calls.last.request
    assert req.url.params["key"] == "KEY" and req.url.params["token"] == "TOKEN"
    assert json.loads(req.read()) == {"idList": "L1", "name": "Имя", "desc": "Описание", "idLabels": "B1,B2"}
    await client.close()


@respx.mock
async def test_client_errors_do_not_leak_secrets():
    respx.get("https://api.trello.com/1/boards/b/lists").mock(return_value=httpx.Response(503, text="down"))
    respx.get("https://api.trello.com/1/boards/b/labels").mock(side_effect=httpx.ConnectError("boom"))
    client = TrelloClient("SECRETKEY", "SECRETTOKEN")
    with pytest.raises(TrelloError) as e1:
        await client.lists("b")
    with pytest.raises(TrelloError) as e2:
        await client.labels("b")
    for err in (e1.value, e2.value):
        assert "SECRET" not in str(err)
    assert "503" in str(e1.value) and "ConnectError" in str(e2.value)
    await client.close()


# --- доска ---


async def test_board_setup_creates_missing_lists_and_labels(db):
    fake = FakeTrello(lists=("Новые запросы",), labels=("ночной",))
    board = await make_sync(db, fake).board()
    assert [lst["name"] for lst in fake.lists_] == ["Новые запросы", "В работе"]
    assert {lbl["name"] for lbl in fake.labels_} == {name for name, _ in LABELS.values()}
    assert board.labels["night"] == "B0"  # существующая метка переиспользована
    assert fake.calls.count("create_label") == 2


# --- синхронизация ---


async def test_dialog_synced_to_card(client: Client, db):
    await complete_dialog(client)
    fake = FakeTrello()
    outbox = Outbox(db, make_sync(db, fake).handlers)
    await outbox.run_once()

    assert await db.outbox_pending() == []
    assert len(fake.cards) == 1
    card = fake.cards["C1"]
    lead = await db.last_lead(USER.id)
    assert lead.trello_card_id == "C1"
    assert card["idList"] == "L0"  # «Новые запросы»
    assert card["name"] == "№1 · Анна Петрова · Квартира · ~18.5 м² · +79001234567"
    assert "**Потолок:** Матовый" in card["desc"] and "анкета заполнена" in card["desc"]
    assert fake.label_names("C1") == {"квалифицирован"}

    msgs = await db.get_messages(lead.id)
    assert len(card["comments"]) == len(msgs)
    assert card["comments"][0].startswith("👤 **Клиент**") and "/start" in card["comments"][0]
    assert "модель: скрипт (без LLM)" in card["comments"][1]
    assert "Нажал кнопку: **Квартира**" in card["comments"][3]


async def test_retry_keeps_order_and_other_leads_proceed(db):
    lead1 = await db.create_lead(tg_user_id=1, chat_id=1, name="Первый", username=None, is_night=False)
    await db.add_message(lead1.id, direction="in", kind="text", text="раз")
    await db.add_message(lead1.id, direction="in", kind="text", text="два")
    lead2 = await db.create_lead(tg_user_id=2, chat_id=2, name="Второй", username=None, is_night=True)
    await db.add_message(lead2.id, direction="in", kind="text", text="привет")

    fake = FakeTrello()
    fake.fail["create_card"] = 1  # первая карточка не создастся
    outbox = Outbox(db, make_sync(db, fake).handlers)
    now = datetime.now(UTC)
    await outbox.run_once(now)

    # Лид 1 ждёт ретрая целиком, лид 2 синхронизирован.
    assert (await db.get_lead(lead1.id)).trello_card_id is None
    card2 = (await db.get_lead(lead2.id)).trello_card_id
    assert card2 and len(fake.cards[card2]["comments"]) == 1
    assert fake.label_names(card2) == {"ночной"}
    pending = await db.outbox_pending()
    assert [t.lead_id for t in pending] == [lead1.id] * 3
    assert pending[0].attempts == 1 and "503" in pending[0].last_error

    await outbox.run_once(now + timedelta(seconds=1))  # ретрай ещё не наступил
    assert len(await db.outbox_pending()) == 3

    await outbox.run_once(now + timedelta(seconds=10))
    card1 = (await db.get_lead(lead1.id)).trello_card_id
    assert [c.split("\n\n")[1] for c in fake.cards[card1]["comments"]] == ["раз", "два"]
    assert await db.outbox_pending() == []


async def test_update_coalesced_and_manual_labels_kept(db):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="Анна", username="anna", is_night=False)
    await db.update_lead(lead.id, object="Дом")
    await db.update_lead(lead.id, area_m2=40.0, area_text="40")
    kinds = [t.kind for t in await db.outbox_pending()]
    assert kinds == [CARD_CREATE, CARD_UPDATE]  # два обновления слились в одно

    fake = FakeTrello(labels=("VIP",))
    outbox = Outbox(db, make_sync(db, fake).handlers)
    await outbox.run_once()
    fake.cards["C1"]["idLabels"].append("B0")  # менеджер вручную поставил VIP

    await db.update_lead(lead.id, status="qualified")
    await outbox.run_once()
    assert fake.label_names("C1") == {"VIP", "квалифицирован"}
    assert fake.cards["C1"]["name"] == "№1 · Анна · Дом · ~40 м²"
    assert "[@anna](https://t.me/anna)" in fake.cards["C1"]["desc"]


async def test_tasks_wait_when_trello_not_configured(db):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await db.add_message(lead.id, direction="in", kind="text", text="привет")
    await Outbox(db, {}).run_once()
    assert [t.kind for t in await db.outbox_pending()] == [CARD_CREATE, CARD_COMMENT]


async def test_enqueue_wakes_worker(db):
    outbox = Outbox(db, {})
    assert not outbox._wake.is_set()
    await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    assert outbox._wake.is_set()


def test_card_name_skips_empty_and_non_phone():
    from app.db import Lead

    lead = Lead(
        id=5, tg_user_id=1, chat_id=1, name=None, username=None, object=None, area_m2=None, area_text="до 15 м²",
        ceiling_type=None, phone="не оставил — связаться в Telegram", measure_time=None, status="new",
        is_night=False, trello_card_id=None, created_at="2026-09-26T20:14:00+00:00",
        updated_at="2026-09-26T20:14:00+00:00", completed_at=None,
    )
    assert card_name(lead) == "№5 · Клиент · до 15 м²"


def test_comment_time_in_studio_zone():
    from app.db import Message

    msg = Message(id=1, lead_id=1, direction="in", kind="voice", text=None, file_id="f", model=None,
                  created_at="2026-09-26T20:14:00+00:00")
    assert comment_text(msg, ZONE) == "👤 **Клиент** · 26.09 23:14\n\n🎤 Голосовое (расшифровка будет ниже)"
