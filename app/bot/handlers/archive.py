"""Выгрузка архива объекта (/archive). Архив собирает воркер (app/worker/exports.py)."""

from datetime import date, timedelta
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.keyboards import with_menu
from app.bot.screens import show
from app.config import Settings
from app.db.models import Site, User
from app.db.repositories import ExportJobRepo, SiteRepo
from app.timeutils import to_local, today_for

router = Router(name="archive")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())

PERIODS = [
    ("today", "Сегодня"),
    ("yesterday", "Вчера"),
    ("7", "7 дней"),
    ("30", "30 дней"),
    ("all", "Всё время"),
]


class ArchiveSite(CallbackData, prefix="ar"):
    site_id: int


class ArchivePeriod(CallbackData, prefix="ap"):
    site_id: int
    period: str


def period_dates(period: str, today: date, site: Site, tz_name: str) -> tuple[date, date]:
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if period in ("7", "30"):
        return today - timedelta(days=int(period) - 1), today
    if period == "all":
        return to_local(site.created_at, tz_name).date(), today
    return today, today


def periods_keyboard(site_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, title in PERIODS:
        kb.button(text=title, callback_data=ArchivePeriod(site_id=site_id, period=code))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _periods_screen(site: Site, can_switch: bool) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder.from_markup(periods_keyboard(site.id))
    if can_switch:
        other = ArchiveSite(site_id=0).pack()
        kb.row(InlineKeyboardButton(text="🏗 Другой объект", callback_data=other))
    return f"🗂 Архив «{escape(site.name)}» — за какой период?", with_menu(kb.as_markup())


async def archive_screen(user: User, session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    sites = await SiteRepo(session).list_for_user(user)
    if not sites:
        return "Объектов пока нет.", with_menu()
    if len(sites) == 1:
        return _periods_screen(sites[0], can_switch=False)
    kb = InlineKeyboardBuilder()
    for site in sites:
        kb.button(text=f"🏗 {site.name}", callback_data=ArchiveSite(site_id=site.id))
    kb.adjust(1)
    return "🗂 Архив какого объекта выгрузить?", with_menu(kb.as_markup())


@router.message(Command("archive"))
async def cmd_archive(message: Message, user: User, session: AsyncSession) -> None:
    text, markup = await archive_screen(user, session)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ArchiveSite.filter())
async def on_archive_site(
    call: CallbackQuery, callback_data: ArchiveSite, user: User, session: AsyncSession
) -> None:
    if callback_data.site_id == 0:
        text, markup = await archive_screen(user, session)
        await show(call, text, markup)
        return
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    await show(call, *_periods_screen(site, can_switch=True))


@router.callback_query(ArchivePeriod.filter())
async def on_archive_period(
    call: CallbackQuery,
    callback_data: ArchivePeriod,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    tz = user.company.timezone
    today = today_for(tz, settings.work_day_start_hour)
    date_from, date_to = period_dates(callback_data.period, today, site, tz)
    chat_id = call.message.chat.id if call.message else call.from_user.id
    created = await ExportJobRepo(session).enqueue(site.id, date_from, date_to, chat_id, user.id)
    if not created:
        text = f"⏳ Архив «{escape(site.name)}» уже собирается — пришлю, как будет готов."
    else:
        period = (
            f"{date_from:%d.%m.%Y}"
            if date_from == date_to
            else f"{date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}"
        )
        text = (
            f"⏳ Собираю архив «{escape(site.name)}» за {period}. Пришлю сюда файлом: "
            "фото, видео и голосовые по дням, реестр сообщений и контрольные суммы."
        )
    await show(call, text, with_menu())
