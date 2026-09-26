from aiogram.fsm.state import State, StatesGroup


class Lead(StatesGroup):
    object = State()
    area = State()
    ceiling_type = State()
    phone = State()
    measure_time = State()
    done = State()


# Порядок вопросов анкеты.
QUESTIONS = [Lead.object, Lead.area, Lead.ceiling_type, Lead.phone, Lead.measure_time]


def next_question(state: str | None) -> State:
    """Следующий вопрос после текущего; после последнего — Lead.done."""
    names = [s.state for s in QUESTIONS]
    i = names.index(state)
    return QUESTIONS[i + 1] if i + 1 < len(QUESTIONS) else Lead.done
