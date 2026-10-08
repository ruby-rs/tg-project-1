"""Главное меню: команда /menu и переходы по разделам кнопками в одном сообщении.

Роутер стоит сразу после согласия — раньше диалогов, ждущих ввод текста: /menu и
кнопки меню прерывают начатый диалог (например, переименование объекта), а не
попадают в него как ответ.
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.handlers import admin, archive, feedback, reports, settings, sites, start
from app.bot.keyboards import MANAGER_SECTIONS, Cancel, Menu, Nav, home_text, main_menu
from app.bot.screens import delete_quietly, drop_reply_keyboard, show
from app.config import Settings
from app.db.models import User

router = Router(name="menu")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())


def home_screen(user: User) -> tuple[str, InlineKeyboardMarkup]:
    return home_text(user), main_menu(user)


async def render(
    to: str, user: User, session: AsyncSession, cfg: Settings
) -> tuple[str, InlineKeyboardMarkup | None]:
    if to in MANAGER_SECTIONS and not user.is_manager:
        return "Этот раздел доступен только руководителю.\n\n" + home_text(user), main_menu(user)
    match to:
        case "report":
            return await reports.report_screen(user, session)
        case "site":
            return await sites.object_screen(user, session)
        case "archive":
            return await archive.archive_screen(user, session)
        case "help":
            return start.help_screen(user)
        case "stats":
            return await feedback.stats_screen(user, session, cfg)
        case "sites":
            return await admin.sites_screen(user, session)
        case "team":
            return await admin.team_screen(user, session)
        case "invite":
            return await start.invite_screen(user, session)
        case "settings":
            return settings.settings_screen(user)
    return home_screen(user)


async def open_menu(message: Message, bot: Bot, user: User, state: FSMContext) -> None:
    await state.clear()
    await delete_quietly(bot, message.chat.id, message.message_id)
    await drop_reply_keyboard(bot, message.chat.id)
    text, markup = home_screen(user)
    await message.answer(text, reply_markup=markup)


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot, user: User, state: FSMContext) -> None:
    await open_menu(message, bot, user, state)


@router.message(F.text.in_(Menu.LEGACY))
async def legacy_button(message: Message, bot: Bot, user: User, state: FSMContext) -> None:
    """Кнопка прежнего меню под полем ввода: убираем его и открываем новое."""
    await open_menu(message, bot, user, state)


@router.callback_query(Nav.filter())
async def on_nav(
    call: CallbackQuery,
    callback_data: Nav,
    user: User,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    text, markup = await render(callback_data.to, user, session, settings)
    await show(call, text, markup)


@router.callback_query(Cancel.filter())
async def on_cancel(
    call: CallbackQuery, callback_data: Cancel, bot: Bot, user: User, state: FSMContext
) -> None:
    await state.clear()
    if callback_data.drop and call.message is not None:
        await call.answer("Отменено")
        await delete_quietly(bot, call.message.chat.id, call.message.message_id)
        return
    text, markup = home_screen(user)
    await show(call, text, markup)
