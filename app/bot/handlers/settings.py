"""Настройки компании (/settings): время сводки и напоминаний, рабочие дни, часовой пояс."""

import re
from datetime import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.filters import IsManager
from app.bot.states import CompanySettings
from app.db.models import Company, User

router = Router(name="settings")
router.message.filter(IsManager())
router.callback_query.filter(IsManager())

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

TIMEZONES = [
    ("Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("Москва (UTC+3)", "Europe/Moscow"),
    ("Самара (UTC+4)", "Europe/Samara"),
    ("Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("Омск (UTC+6)", "Asia/Omsk"),
    ("Новосибирск (UTC+7)", "Asia/Novosibirsk"),
    ("Красноярск (UTC+7)", "Asia/Krasnoyarsk"),
    ("Иркутск (UTC+8)", "Asia/Irkutsk"),
    ("Якутск (UTC+9)", "Asia/Yakutsk"),
    ("Владивосток (UTC+10)", "Asia/Vladivostok"),
    ("Магадан (UTC+11)", "Asia/Magadan"),
    ("Камчатка (UTC+12)", "Asia/Kamchatka"),
]

OFF_WORDS = {"выкл", "выключить", "нет", "off", "-", "—"}


class SettingsAction(CallbackData, prefix="st"):
    action: str  # card | digest | reminder | days | day | tz | tzset
    value: int = 0


def parse_time(text: str) -> time | None:
    """«19:30», «19.30», «1930», «19» → time. ValueError — не похоже на время."""
    raw = text.strip().lower()
    if raw in OFF_WORDS:
        return None
    m = re.fullmatch(r"(\d{1,2})(?:[:.\s]?(\d{2}))?", raw)
    if not m:
        raise ValueError(raw)
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    return time(hour, minute)  # ValueError, если часы/минуты вне диапазона


def _fmt_time(value: time | None) -> str:
    return value.strftime("%H:%M") if value else "выключено"


def _tz_title(tz: str) -> str:
    return next((title for title, name in TIMEZONES if name == tz), tz)


def settings_text(company: Company) -> str:
    days = ", ".join(WEEKDAYS[int(d) - 1] for d in sorted(company.work_days)) or "не выбраны"
    return "\n".join(
        [
            f"⚙️ <b>Настройки «{escape(company.name)}»</b>",
            "",
            f"🗓 Вечерняя сводка руководителям: {_fmt_time(company.digest_time)}",
            f"⏰ Напоминание прорабам без сообщений: {_fmt_time(company.reminder_time)}",
            f"📅 Рабочие дни: {days}",
            f"🌍 Часовой пояс: {escape(_tz_title(company.timezone))}",
            "",
            "Напоминания приходят только в рабочие дни. Сводка в выходной придёт, "
            "только если кто-то присылал сообщения.",
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗓 Время сводки", callback_data=SettingsAction(action="digest"))
    kb.button(text="⏰ Время напоминания", callback_data=SettingsAction(action="reminder"))
    kb.button(text="📅 Рабочие дни", callback_data=SettingsAction(action="days"))
    kb.button(text="🌍 Часовой пояс", callback_data=SettingsAction(action="tz"))
    kb.adjust(2, 2)
    return kb.as_markup()


def days_keyboard(work_days: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(WEEKDAYS, start=1):
        mark = "✅" if str(i) in work_days else "▫️"
        kb.button(text=f"{mark} {name}", callback_data=SettingsAction(action="day", value=i))
    kb.button(text="← Готово", callback_data=SettingsAction(action="card"))
    kb.adjust(4, 3, 1)
    return kb.as_markup()


def tz_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, (title, _) in enumerate(TIMEZONES):
        kb.button(text=title, callback_data=SettingsAction(action="tzset", value=i))
    kb.button(text="← Назад", callback_data=SettingsAction(action="card"))
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("settings"))
async def cmd_settings(message: Message, user: User) -> None:
    await message.answer(settings_text(user.company), reply_markup=settings_keyboard())


@router.callback_query(SettingsAction.filter())
async def on_settings_action(
    call: CallbackQuery, callback_data: SettingsAction, user: User, state: FSMContext
) -> None:
    company = user.company
    action = callback_data.action
    await call.answer()
    if call.message is None:
        return

    if action in ("digest", "reminder"):
        await state.set_state(
            CompanySettings.digest_time if action == "digest" else CompanySettings.reminder_time
        )
        what = "вечернюю сводку" if action == "digest" else "напоминание прорабам"
        await call.message.answer(
            f"Во сколько присылать {what}? Напишите, например, <b>19:00</b> "
            "или «выкл», чтобы отключить. Отменить — /cancel"
        )
        return

    if action == "days":
        await call.message.edit_text(
            "Отметьте рабочие дни:", reply_markup=days_keyboard(company.work_days)
        )
        return

    if action == "day" and 1 <= callback_data.value <= 7:
        day = str(callback_data.value)
        days = set(company.work_days)
        days.symmetric_difference_update({day})
        company.work_days = "".join(sorted(days))
        await call.message.edit_reply_markup(reply_markup=days_keyboard(company.work_days))
        return

    if action == "tz":
        await call.message.edit_text("Выберите часовой пояс компании:", reply_markup=tz_keyboard())
        return

    if action == "tzset" and 0 <= callback_data.value < len(TIMEZONES):
        company.timezone = TIMEZONES[callback_data.value][1]

    await call.message.edit_text(settings_text(company), reply_markup=settings_keyboard())


@router.message(CompanySettings.digest_time, F.text)
@router.message(CompanySettings.reminder_time, F.text)
async def on_time_input(message: Message, user: User, state: FSMContext) -> None:
    try:
        value = parse_time(message.text)
    except ValueError:
        await message.answer("Не понял время. Напишите, например, 19:00 или «выкл».")
        return
    if await state.get_state() == CompanySettings.digest_time.state:
        user.company.digest_time = value
    else:
        user.company.reminder_time = value
    await state.clear()
    await message.answer(
        "✅ Сохранено.\n\n" + settings_text(user.company), reply_markup=settings_keyboard()
    )
