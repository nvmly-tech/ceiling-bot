"""Клиент удалил заявку: сообщения о ней в группе менеджеров удаляются (Telegram разрешает — до 48 ч)."""

from datetime import UTC, datetime, timedelta

from app.db import TG_DIGEST
from tests.test_notifier import Env, make_env
from tests.test_trello import complete_dialog


async def group_message_ids(db, lead_id: int) -> set[int]:
    return {m.message_id for m in await db.tg_messages(lead_id)}


async def lead_with_two_notifications(db) -> Env:
    env = await make_env(db)
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)  # «Новая заявка»
    await env.client.text("ещё хочу подсветку")
    await env.tick(t0 + timedelta(seconds=61))  # «клиент дописал»
    return env


async def test_group_messages_deleted_with_lead(db):
    env = await lead_with_two_notifications(db)
    ids = await group_message_ids(db, 1)
    assert len(ids) == 2  # бот запомнил оба своих сообщения о заявке

    await db.delete_lead_data(1)
    await env.tick()
    assert set(env.session.deleted()) == ids
    assert "Заявка №1 удалена клиентом" in env.group()[-1].text  # уведомление об удалении остаётся
    assert await db.tg_messages(1) == []


async def test_messages_older_than_48h_stay(db):
    env = await lead_with_two_notifications(db)
    old = (datetime.now(UTC) - timedelta(hours=49)).isoformat(timespec="seconds")
    await db.conn.execute("UPDATE tg_messages SET created_at = ?", (old,))
    await db.conn.commit()
    await db.delete_lead_data(1)
    await env.tick()
    assert env.session.deleted() == []  # Telegram уже не даст удалить — пусть висят
    assert "Заявка №1 удалена клиентом" in env.group()[-1].text


async def test_telegram_refusal_does_not_block_notice(db):
    env = await lead_with_two_notifications(db)
    env.session.undeletable = await group_message_ids(db, 1)  # например, удалили руками раньше
    await db.delete_lead_data(1)
    await env.tick()
    assert "Заявка №1 удалена клиентом" in env.group()[-1].text
    assert not [t for t in await db.outbox_pending()]  # задача не застряла в ретраях


async def test_digest_drops_deleted_lead_or_is_deleted(db):
    env = await make_env(db)
    a = await db.create_lead(tg_user_id=1, chat_id=1, name="Первый", username=None, is_night=True)
    b = await db.create_lead(tg_user_id=2, chat_id=2, name="Второй", username=None, is_night=True)
    for lead in (a, b):
        await db.update_lead(lead.id, status="qualified", notified_status="qualified",
                             notified_at=datetime.now(UTC).isoformat(timespec="seconds"))
    await db.enqueue(TG_DIGEST, None, {"lead_ids": [a.id, b.id]})
    await env.outbox.run_once()
    [digest_id] = await group_message_ids(db, a.id)
    assert await group_message_ids(db, b.id) == {digest_id}  # одна сводка — на обе заявки

    await db.delete_lead_data(a.id)
    await env.outbox.run_once()
    edit = env.session.edits()[-1]
    assert edit.message_id == digest_id and "№2</b> Второй" in edit.text and "Первый" not in edit.text
    assert [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row] == [f"take:{b.id}"]
    assert digest_id not in env.session.deleted()  # сводку с другой заявкой не удаляем

    await db.delete_lead_data(b.id)
    await env.outbox.run_once()
    assert digest_id in env.session.deleted()  # удалённых заявок в ней не осталось — удаляем целиком


async def test_take_button_on_old_message_of_deleted_lead(db):
    env = await lead_with_two_notifications(db)
    old = (datetime.now(UTC) - timedelta(hours=49)).isoformat(timespec="seconds")
    await db.conn.execute("UPDATE tg_messages SET created_at = ?", (old,))
    await db.conn.commit()
    notification = [m for m in env.group() if "Новая заявка" in m.text][0]  # осталось висеть (> 48 ч)
    await db.delete_lead_data(1)
    await env.tick()

    await env.client.press_in_group(notification, "take:1")
    answer = [c for c in env.session.calls if type(c).__name__ == "AnswerCallbackQuery"][-1]
    assert answer.text == "Заявка №1 удалена клиентом" and answer.show_alert
    assert (await db.get_lead(1)).taken_at is None
    assert [t.kind for t in await db.outbox_pending()] == []  # в Trello ничего не уходит
    [markup_edit] = [c for c in env.session.calls if type(c).__name__ == "EditMessageReplyMarkup"]
    assert markup_edit.reply_markup is None  # кнопку «Взял» убрали


async def test_message_left_in_old_group_after_migration(db):
    env = await lead_with_two_notifications(db)
    env.session.migrate[-5000] = -100777  # группу превратили в супергруппу после уведомлений
    await db.delete_lead_data(1)
    await env.tick()
    # Номера сообщений в новой супергруппе могут не совпадать — удалять «наугад» нельзя: пропускаем.
    deletes = [c for c in env.session.calls if type(c).__name__ == "DeleteMessage"]
    assert {c.chat_id for c in deletes} == {-5000}  # только в старом чате, в новый — не угадываем
    assert not await db.outbox_pending()  # и задача не застряла в ретраях
    assert "Заявка №1 удалена клиентом" in env.session.sent(-100777)[-1].text
