"""Главное меню под полем ввода: кнопки вместо команд.

Роутер стоит сразу после согласия — раньше диалогов, ждущих ввод текста: нажатие
кнопки меню прерывает начатый диалог (например, переименование объекта), а не
попадает в него как ответ. И раньше приёма сообщений — текст кнопки не сохраняется
как сообщение с объекта.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany, IsManager
from app.bot.handlers import admin, archive, feedback, settings, sites, start
from app.bot.handlers.reports import show_report_menu
from app.bot.keyboards import Cancel, Menu, main_menu
from app.config import Settings
from app.db.models import User

router = Router(name="menu")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())


@router.message(F.text == Menu.REPORT)
async def menu_report(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    await show_report_menu(message, user, session)


@router.message(F.text == Menu.SITE)
async def menu_site(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await sites.cmd_object(message, user, session)


@router.message(F.text == Menu.ARCHIVE)
async def menu_archive(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    await archive.cmd_archive(message, user, session)


@router.message(F.text == Menu.HELP)
async def menu_help(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await start.cmd_help(message, user)


@router.message(F.text == Menu.STATS, IsManager())
async def menu_stats(
    message: Message, user: User, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    await feedback.cmd_stats(message, user, session, settings)


@router.message(F.text == Menu.SITES, IsManager())
async def menu_sites(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    await admin.cmd_sites(message, user, session)


@router.message(F.text == Menu.TEAM, IsManager())
async def menu_team(message: Message, user: User, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await admin.cmd_team(message, user, session)


@router.message(F.text == Menu.INVITE, IsManager())
async def menu_invite(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    await start.cmd_invite(message, user, session)


@router.message(F.text == Menu.SETTINGS, IsManager())
async def menu_settings(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await settings.cmd_settings(message, user)


@router.message(F.text.in_(Menu.MANAGER_ONLY))
async def menu_manager_only(message: Message, user: User, state: FSMContext) -> None:
    """Кнопка руководителя у прораба — меню осталось от прежней роли."""
    await state.clear()
    await message.answer(
        "Это доступно только руководителю. Обновил меню под вашу роль.",
        reply_markup=main_menu(user),
    )


@router.callback_query(Cancel.filter())
async def on_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    if call.message:
        await call.message.edit_text("Отменено.")
