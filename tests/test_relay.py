"""Ответ клиенту через бота: менеджер отвечает в группе на подсказку бота — бот пересылает клиенту.

Нужно, потому что «Написать клиенту» работает только с @username, а клиент, выбравший «напишите мне
в Telegram», без него для менеджера недостижим. Пока идёт разговор с человеком, бот клиенту не отвечает.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage, SetMessageReaction
from aiogram.types import Message, PhotoSize, Update

from app.db import Database, now_iso
from tests.conftest import CHAT, MANAGER, MANAGER_CHAT, USER, FakeSession
from tests.test_notifier import GROUP, Env, make_env
from tests.test_trello import complete_dialog


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db)


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] if markup else []


async def qualified(env: Env) -> None:
    await complete_dialog(env.client)
    await env.tick()


async def prompt_id(env: Env, lead_id: int = 1) -> int:
    [m] = [m for m in await env.db.tg_messages(lead_id) if m.kind == "reply_prompt"]
    return m.message_id


async def manager_says(env: Env, reply_to: int | None, text: str | None = None, photo: str | None = None,
                       user=MANAGER) -> None:
    """Менеджер пишет в группе — ответом на сообщение reply_to (или просто так)."""
    env.client._update_id += 1
    target = Message(message_id=reply_to, date=datetime.now(UTC), chat=MANAGER_CHAT, text="…") if reply_to else None
    extra = {"photo": [PhotoSize(file_id=photo, file_unique_id=photo, width=10, height=10)], "caption": text} \
        if photo else {"text": text}
    msg = Message(message_id=700 + env.client._update_id, date=datetime.now(UTC), chat=MANAGER_CHAT, from_user=user,
                  reply_to_message=target, **extra)
    await env.client.dp.feed_update(env.client.bot, Update(update_id=env.client._update_id, message=msg))


async def test_reply_button_everywhere_managers_look(env: Env):
    await qualified(env)
    assert "reply:1" in buttons(env.group()[0].reply_markup)  # уведомление о заявке
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.tick()
    panel = [m for m in env.group() if m.text.startswith("📋")][-1]
    assert "reply:1" in buttons(panel.reply_markup)


async def test_manager_reply_reaches_client_and_transcript(env: Env):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    prompt = env.group()[-1]
    assert prompt.text.startswith("✍️") and "№1" in prompt.text
    assert prompt.reply_markup.force_reply and prompt.reply_markup.selective

    await manager_says(env, await prompt_id(env), "Анна, добрый день! Удобно созвониться в 18:00?")
    to_client = env.session.sent(CHAT.id)[-1]
    assert to_client.text == "Иван, менеджер студии:\nАнна, добрый день! Удобно созвониться в 18:00?"
    assert [c for c in env.session.calls if isinstance(c, SetMessageReaction)]  # 👍 под ответом менеджера
    msg = (await env.db.get_messages(1, direction="out"))[-1]
    assert (msg.kind, msg.model) == ("manager", "Иван Менеджеров")
    await env.tick()
    assert env.trello.cards["C1"]["comments"][-1].startswith("👔 **Менеджер Иван Менеджеров**")


async def test_manager_photo_is_copied(env: Env):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    await manager_says(env, await prompt_id(env), "Вот образцы", photo="photo-samples")
    assert env.session.sent(CHAT.id)[-1].text == "Иван, менеджер студии:"
    [copy] = env.session.copies(CHAT.id)
    assert copy.from_chat_id == GROUP
    msg = (await env.db.get_messages(1, direction="out"))[-1]
    assert msg.file_id == "photo-samples" and "образцы" in msg.text  # в карточку — вложением


async def test_only_replies_to_the_prompt_go_to_client(env: Env):
    await qualified(env)
    n = len(env.session.sent(CHAT.id))
    await manager_says(env, None, "Коллеги, кто возьмёт №1?")
    await manager_says(env, 1001, "это ответ на уведомление, а не на подсказку")
    assert len(env.session.sent(CHAT.id)) == n


async def test_reply_for_deleted_lead_is_not_sent(env: Env):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    pid = await prompt_id(env)
    await env.db.delete_lead_data(1)
    n = len(env.session.sent(CHAT.id))
    await manager_says(env, pid, "Анна, вы тут?")
    assert len(env.session.sent(CHAT.id)) == n
    assert "удалена" in env.group()[-1].text


async def test_blocked_client_reported_to_manager(env: Env, monkeypatch):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    original = FakeSession.make_request

    async def blocked(self, bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == CHAT.id:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        return await original(self, bot, method, timeout)

    monkeypatch.setattr(FakeSession, "make_request", blocked)
    await manager_says(env, await prompt_id(env), "Анна, добрый день!")
    assert "не доставлено" in env.group()[-1].text.lower()


async def test_while_manager_talks_bot_stays_silent(env: Env):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    await manager_says(env, await prompt_id(env), "Удобно в 18:00?")
    n = len(env.session.sent(CHAT.id))
    await env.client.text("да, удобно")
    assert len(env.session.sent(CHAT.id)) == n  # ни ответа LLM/скрипта, ни «Передал менеджеру»
    await env.tick(datetime.now(UTC) + timedelta(seconds=10))
    answer = env.group()[-1]
    assert "ответил(а)" in answer.text and "да, удобно" in answer.text and "reply:1" in buttons(answer.reply_markup)


async def test_manager_can_talk_to_client_who_left_questionnaire(env: Env):
    """Клиент бросил анкету на втором вопросе; ответ на сообщение менеджера — не площадь и не новый вопрос."""
    await env.client.text("/start")
    await env.client.press("obj:flat")
    await env.db.update_lead(1, status="abandoned")
    await env.tick()
    await env.client.press_in_group(env.group()[0], "reply:1")
    await manager_says(env, await prompt_id(env), "Анна, подскажите площадь — посчитаем по телефону")
    await env.client.text("метров двадцать, лучше позвоните")
    lead = await env.db.get_lead(1)
    assert lead.area_m2 is None and lead.area_text is None
    await env.tick(datetime.now(UTC) + timedelta(seconds=10))
    answer = env.group()[-1]
    assert "метров двадцать" in answer.text and "Квартира" not in answer.text  # прежние ответы анкеты — не повторяем


async def test_conversation_window_expires(env: Env):
    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1")
    await manager_says(env, await prompt_id(env), "Удобно в 18:00?")
    await env.db.update_lead(1, manager_reply_at=now_iso(datetime.now(UTC) - timedelta(hours=13)))
    n = len(env.session.sent(CHAT.id))
    await env.client.text("а сколько стоит глянец?")
    assert len(env.session.sent(CHAT.id)) == n + 1  # бот снова отвечает сам


async def test_reply_button_checks(env: Env):
    from aiogram.types import Chat

    await qualified(env)
    await env.client.press_in_group(env.group()[0], "reply:1", chat=Chat(id=-999, type="group"))
    await env.client.press_in_group(env.group()[0], "reply:x")
    await env.client.press_in_group(env.group()[0], "reply:99")
    assert not [m for m in env.group() if m.text.startswith("✍️")]
    assert USER.id == CHAT.id
