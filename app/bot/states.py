from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    company_name = State()


class SiteCreation(StatesGroup):
    name = State()
