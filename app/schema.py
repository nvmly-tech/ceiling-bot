"""Схема базы: таблицы, колонки, добавленные после первой версии, и разовое заполнение старых строк.

Новые колонки дописываются в существующую базу при старте (Database._migrate) — пересоздавать её не нужно.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id    INTEGER NOT NULL,
    chat_id       INTEGER NOT NULL,
    name          TEXT,
    username      TEXT,
    object        TEXT,
    area_m2       REAL,
    area_text     TEXT,
    ceiling_type  TEXT,
    phone         TEXT,
    measure_time  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',   -- new | qualified | abandoned | cancelled (закрыта клиентом) | deleted
    is_night      INTEGER NOT NULL DEFAULT 0,
    trello_card_id TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS leads_user ON leads(tg_user_id, id);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    direction  TEXT NOT NULL,          -- in | out
    kind       TEXT NOT NULL,          -- text | voice | contact | button | photo | document | other
    text       TEXT,
    file_id    TEXT,
    model      TEXT,                   -- кто сформировал ответ бота: script | <llm>
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id, id);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    lead_id         INTEGER,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    done_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(done_at, id);

-- Сообщения бота о заявке в группе менеджеров: чтобы удалить их, если клиент удалит заявку.
CREATE TABLE IF NOT EXISTS tg_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,          -- lead | remind | client | digest (строка на каждую заявку) | manager | panel
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tg_messages_lead ON tg_messages(lead_id);

CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Колонки, добавленные после первой версии схемы: дописываются в существующую базу при старте.
MIGRATIONS = [
    ("leads", "trello_card_url", "TEXT"),
    ("leads", "notified_status", "TEXT"),          # статус, о котором менеджер уже уведомлён
    ("leads", "notified_at", "TEXT"),
    ("leads", "taken_by_id", "INTEGER"),
    ("leads", "taken_by_name", "TEXT"),
    ("leads", "taken_at", "TEXT"),
    ("leads", "reminders_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "last_reminder_at", "TEXT"),
    ("leads", "client_msgs_notified", "INTEGER NOT NULL DEFAULT 0"),  # id последнего сообщения, о котором сказали
    ("outbox", "queue", "TEXT"),                   # задачи одной очереди выполняются строго по порядку
    ("leads", "summary", "TEXT"),                  # резюме лида от LLM для менеджера
    ("leads", "hotness", "TEXT"),                  # горячий | тёплый | холодный
    ("leads", "hotness_reason", "TEXT"),
    ("leads", "summary_model", "TEXT"),            # какая модель написала резюме
    ("leads", "summary_status", "TEXT"),           # для какого статуса лида написано резюме
    ("leads", "llm_calls", "INTEGER NOT NULL DEFAULT 0"),  # обращений к LLM по заявке (бюджет токенов)
    ("messages", "text_alt", "TEXT"),              # теневая расшифровка голосового другой моделью (для сравнения)
    ("messages", "text_alt_model", "TEXT"),
    ("leads", "stage", "TEXT"),                    # этап после «Взял»: app/stages.py (NULL — итога ещё нет)
    ("leads", "stage_at", "TEXT"),
    ("leads", "stage_by_name", "TEXT"),            # кто из менеджеров отметил этап
    ("leads", "measure_at", "TEXT"),               # на когда назначен замер (UTC)
    ("leads", "refuse_reason", "TEXT"),            # код причины отказа (stages.REFUSE_REASONS)
    ("leads", "taken_by_username", "TEXT"),        # @username взявшего — упомянуть в напоминании
    ("leads", "nudges_sent", "INTEGER NOT NULL DEFAULT 0"),  # напоминаний о заявке без итога на текущем этапе
    ("leads", "last_nudge_at", "TEXT"),
    ("leads", "escalated_at", "TEXT"),             # когда сообщили владельцу (на текущем этапе)
    ("leads", "measure_reminded_for", "TEXT"),     # measure_at, о котором клиенту уже напомнили накануне
    ("leads", "contact_asked_at", "TEXT"),         # когда спросили клиента «с вами связался менеджер?»
    ("leads", "contact_answer", "TEXT"),           # yes | no
    ("leads", "rating_asked_at", "TEXT"),          # когда попросили оценить замер
    ("leads", "rating", "INTEGER"),                # оценка замера клиентом, 1–5
    ("leads", "source", "TEXT"),                   # метка источника из ссылки t.me/<бот>?start=<метка>
    ("leads", "visit_answer", "TEXT"),             # ответ клиента о назначенном замере: yes | move | cancel
    ("leads", "manager_reply_at", "TEXT"),         # когда менеджер последний раз ответил клиенту через бота
]

NUDGES_OFF = 1000  # «напоминания уже исчерпаны»: так помечены заявки, взятые до появления напоминаний
# Выполняется один раз — когда колонка появляется в существующей базе. Без этого после обновления бота
# все давно взятые заявки разом получили бы напоминания и эскалации владельцу.
BACKFILL = {
    ("leads", "escalated_at"): f"UPDATE leads SET escalated_at = created_at, nudges_sent = {NUDGES_OFF}",
    # Клиентов по давно взятым заявкам и давно прошедшим замерам вопросами не беспокоим.
    ("leads", "contact_asked_at"): "UPDATE leads SET contact_asked_at = created_at WHERE taken_at IS NOT NULL",
    ("leads", "rating_asked_at"): (
        "UPDATE leads SET rating_asked_at = created_at"
        " WHERE measure_at IS NOT NULL AND measure_at < strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
    ),
}
