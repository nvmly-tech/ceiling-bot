"""Отчёт владельцу по заявкам за период: сколько пришло, как быстро брали, чем закончились, что сказали клиенты.

Считается по базе, без LLM. Период — заявки, созданные в нём (их текущее состояние). Недельный отчёт уходит сам
в первый рабочий час новой недели — за прошлую неделю (пн–вс); за любой срок — командой /report.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from html import escape
from statistics import median

from app.config import Settings
from app.db import TG_REPORT, Database, Lead, OutboxTask, now_iso
from app.services.inwork import LOW_RATING, RATING_MAX
from app.services.notifier import HOT_ICONS, Notifier
from app.stages import CONTRACT, MEASURE, NO_ANSWER, REFUSE_REASONS, REFUSED, STAGE_ICONS, THINKING
from app.worktime import is_work_time, next_work_start

KV_LAST_REPORT = "last_report_week"  # понедельник недели, в которую отчёт уже отправлен (или бот впервые запущен)
REPORT_DAYS_DEFAULT = 7
REPORT_DAYS_MAX = 90
NO_SOURCE = "без метки"  # заявка пришла не по размеченной ссылке
LEADS = ("заявка", "заявки", "заявок")
RATINGS = ("оценка", "оценки", "оценок")
DAYS = ("день", "дня", "дней")
HOT_FORMS = {"горячий": "горячих", "тёплый": "тёплых", "холодный": "холодных"}
STAGE_LABELS = {
    CONTRACT: "договор", MEASURE: "замер назначен", THINKING: "думают", NO_ANSWER: "не дозвонились", None: "без итога",
}


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """«1 заявка», «2 заявки», «5 заявок»."""
    if n % 10 == 1 and n % 100 != 11:
        form = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{n} {form}"


def _numbers(leads: Sequence[Lead]) -> str:
    return ", ".join(f"№{lead.id}" for lead in leads)


def _joined(parts: dict[str, int]) -> str:
    """«a: 1 · b: 2» — без нулевых."""
    return " · ".join(f"{label}: {n}" for label, n in parts.items() if n)


def minutes_to_take(lead: Lead, settings: Settings) -> int:
    """Сколько минут рабочего времени заявка ждала «Взял»: ночная заявка ждёт с начала рабочего дня."""
    start = next_work_start(datetime.fromisoformat(lead.notified_at), settings)
    return max(0, int((datetime.fromisoformat(lead.taken_at) - start).total_seconds() // 60))


def _leads_section(leads: Sequence[Lead], repeats: int) -> list[str]:
    status = Counter(lead.status for lead in leads)
    hot = Counter(lead.hotness for lead in leads)
    kinds = {f"{HOT_ICONS[h]} {HOT_FORMS[h]}": hot[h] for h in HOT_FORMS}
    kinds["🌙 ночных"] = sum(lead.is_night for lead in leads)
    kinds["🔁 повторных"] = repeats
    lines = [f"<b>Заявок:</b> {len(leads)}", "• " + _joined({
        "анкета заполнена": status["qualified"], "не завершили": status["abandoned"] + status["new"],
        "закрыты клиентом": status["cancelled"] + status["deleted"],
    })]
    if any(kinds.values()):
        lines.append("• " + _joined(kinds))
    sources = Counter(lead.source or NO_SOURCE for lead in leads)
    if set(sources) != {NO_SOURCE}:
        ranked = sorted(sources.items(), key=lambda kv: (kv[0] == NO_SOURCE, -kv[1], kv[0]))  # «без метки» — в конце
        lines.append("• источники: " + " · ".join(f"{escape(name)} {n}" for name, n in ranked))
    return lines


def _reaction_section(leads: Sequence[Lead], settings: Settings) -> list[str]:
    """Как быстро менеджеры брали заявки, о которых бот их уведомил."""
    notified = [lead for lead in leads if lead.notified_at and lead.status not in ("cancelled", "deleted")]
    if not notified:
        return []
    taken = [lead for lead in notified if lead.taken_at]
    waits = {lead.id: minutes_to_take(lead, settings) for lead in taken}
    lines = ["", "<b>Реакция менеджеров</b>"]
    usual = f", обычно через {int(median(waits.values()))} мин" if waits else ""
    lines.append(f"• взяли: {len(taken)} из {len(notified)}{usual}")
    untaken = [lead for lead in notified if not lead.taken_at]
    if untaken:
        lines.append(f"• не взяли: {len(untaken)} ({_numbers(untaken)})")
    by_manager = Counter(lead.taken_by_name for lead in taken)
    for name, count in by_manager.most_common():
        own = [waits[lead.id] for lead in taken if lead.taken_by_name == name]
        lines.append(f"• {escape(name or '—')} — {plural(count, LEADS)}, обычно через {int(median(own))} мин")
    return lines


def _outcomes_section(leads: Sequence[Lead]) -> list[str]:
    taken = [lead for lead in leads if lead.taken_at and lead.status not in ("cancelled", "deleted")]
    if not taken:
        return []
    stage = Counter(lead.stage for lead in taken)
    lines = ["", "<b>Итоги</b>",
             "• " + _joined({f"{STAGE_ICONS[s]} {label}": stage[s] for s, label in STAGE_LABELS.items()})]
    if stage[REFUSED]:
        reasons = Counter(lead.refuse_reason for lead in taken if lead.stage == REFUSED)
        why = ", ".join(f"{REFUSE_REASONS.get(r, r or 'без причины')} {n}" for r, n in reasons.most_common())
        lines.append(f"• {STAGE_ICONS[REFUSED]} отказ: {stage[REFUSED]} — {why}")
    return lines


def _clients_section(leads: Sequence[Lead]) -> list[str]:
    not_called = [lead for lead in leads if lead.contact_answer == "no"]
    ratings = [lead.rating for lead in leads if lead.rating]
    if not not_called and not ratings:
        return []
    lines = ["", "<b>Клиенты</b>"]
    if not_called:
        lines.append(f"• «с нами не связались»: {len(not_called)} ({_numbers(not_called)})")
    if ratings:
        average = f"{sum(ratings) / len(ratings):.1f}".removesuffix(".0")
        low = sum(r <= LOW_RATING for r in ratings)
        lines.append(f"• оценка замера: {average} из {RATING_MAX} ({plural(len(ratings), RATINGS)}), низких: {low}")
    return lines


def report_text(
    leads: Sequence[Lead], since: datetime, until: datetime, settings: Settings, *, label: str | None = None,
    repeats: int = 0,
) -> str:
    """Текст отчёта (HTML для Telegram). label — подпись периода вместо дат («за 7 дней»);
    repeats — сколько заявок от клиентов, которые уже обращались."""
    zone = settings.zone
    last_day = (until - timedelta(seconds=1)).astimezone(zone)
    dates = f"{since.astimezone(zone):%d.%m}–{last_day:%d.%m}"
    header = f"📊 <b>Отчёт по заявкам</b> · {f'{label} ({dates})' if label else dates}"
    if not leads:
        return f"{header}\n\nЗаявок за период не было."
    sections = [
        _leads_section(leads, repeats), _reaction_section(leads, settings), _outcomes_section(leads),
        _clients_section(leads),
    ]
    return "\n".join([header, "", *(line for section in sections for line in section)])


class Reports:
    def __init__(self, db: Database, settings: Settings, notifier: Notifier):
        self.db, self.settings, self.notifier = db, settings, notifier

    @property
    def handlers(self):
        return {TG_REPORT: self.send}

    async def text(self, since: datetime, until: datetime, *, label: str | None = None) -> str:
        leads = await self.db.leads_created_between(since, until)
        live = [lead for lead in leads if lead.status not in ("cancelled", "deleted")]
        repeats = sum([bool(await self.db.previous_leads(lead)) for lead in live])
        return report_text(leads, since, until, self.settings, label=label, repeats=repeats)

    async def for_days(self, days: int) -> str:
        """Отчёт за последние days дней — для команды /report."""
        # Время в базе — до секунды, а граница периода строгая: +1 с, чтобы вошла и только что созданная заявка.
        until = datetime.now(UTC) + timedelta(seconds=1)
        return await self.text(until - timedelta(days=days), until, label=f"за {plural(days, DAYS)}")

    async def send(self, task: OutboxTask) -> None:
        since, until = (datetime.fromisoformat(task.payload[k]) for k in ("since", "until"))
        await self.notifier.to_owner(await self.text(since, until))

    async def scan(self, now: datetime) -> None:
        """Раз в неделю, в первый рабочий час новой недели, — отчёт за прошлую. Неделю первого запуска
        пропускаем: отчёт «за прошлую неделю» сразу после установки бота никому не нужен."""
        s = self.settings
        local = now.astimezone(s.zone)
        if not s.weekly_report or not is_work_time(local, s.work_start, s.work_end):
            return
        monday = local.date() - timedelta(days=local.weekday())
        last = await self.db.kv_get(KV_LAST_REPORT)
        if last == monday.isoformat():
            return
        await self.db.kv_set(KV_LAST_REPORT, monday.isoformat())
        if last is None:
            return
        until = datetime.combine(monday, time(0), tzinfo=s.zone).astimezone(UTC)
        since = until - timedelta(days=7)
        await self.db.enqueue(TG_REPORT, None, {"since": now_iso(since), "until": now_iso(until)})
