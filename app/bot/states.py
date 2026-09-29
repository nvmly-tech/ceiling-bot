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


class EditLead(StatesGroup):
    """Клиент правит поле своей заявки (/order)."""

    object = State()
    area = State()
    ceiling_type = State()
    phone = State()
    measure_time = State()


EDITS = {
    "object": EditLead.object, "area": EditLead.area, "ceiling_type": EditLead.ceiling_type,
    "phone": EditLead.phone, "measure_time": EditLead.measure_time,
}

