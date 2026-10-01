from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    company_name = State()


class SiteCreation(StatesGroup):
    name = State()


class SiteEdit(StatesGroup):
    name = State()
    address = State()


class CompanySettings(StatesGroup):
    digest_time = State()
    reminder_time = State()
