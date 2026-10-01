"""Запрос отчёта. Сам отчёт собирает воркер (app/worker/reports.py) и присылает в чат."""

from collections import Counter
from datetime import date, timedelta
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.keyboards import ReportSite, ReportView, feedback_keyboard, report_sites_keyboard
from app.config import Settings
from app.db.models import Site, User
from app.db.repositories import EntryRepo, ReportJobRepo, ReportRepo, SiteRepo
from app.reports.render import render_report, split_message
from app.reports.schema import SiteDailyReport
from app.timeutils import today_for

router = Router(name="reports")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())

_DAYS_AGO_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}


def parse_days_ago(args: str | None) -> int:
    if not args:
        return 0
    arg = args.strip().lower()
    if arg in _DAYS_AGO_WORDS:
        return _DAYS_AGO_WORDS[arg]
    if arg.isdigit():
        return min(int(arg), 60)
    return 0


async def request_report(
    bot: Bot,
    chat_id: int,
    site: Site,
    days_ago: int,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    work_date = today_for(user.company.timezone, settings.work_day_start_hour) - timedelta(
        days=days_ago
    )
    created = await ReportJobRepo(session).enqueue(site.id, work_date, chat_id, user.id)
    what = f"по «{escape(site.name)}» за {work_date:%d.%m.%Y}"
    if created:
        text = f"⏳ Формирую отчёт {what}. Пришлю сюда, как будет готов."
    else:
        text = f"⏳ Отчёт {what} уже формируется — пришлю, как будет готов."
    await bot.send_message(chat_id, text)


@router.message(Command("report"))
async def cmd_report(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    days_ago = parse_days_ago(command.args)
    repo = SiteRepo(session)
    current = await repo.get_for_user(user, user.current_site_id) if user.current_site_id else None
    if current is not None and not user.is_manager:
        await request_report(bot, message.chat.id, current, days_ago, user, session, settings)
        return

    sites = await repo.list_for_user(user)
    if not sites:
        await message.answer("Объектов пока нет. Добавьте первый: /new_object")
    elif len(sites) == 1:
        await request_report(bot, message.chat.id, sites[0], days_ago, user, session, settings)
    else:
        await message.answer(
            "По какому объекту отчёт?", reply_markup=report_sites_keyboard(sites, days_ago)
        )


@router.callback_query(ReportSite.filter())
async def on_report_site(
    call: CallbackQuery,
    callback_data: ReportSite,
    bot: Bot,
    user: User,
    session: AsyncSession,
    settings: Settings,
) -> None:
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    await call.answer()
    chat_id = call.message.chat.id if call.message else call.from_user.id
    await request_report(bot, chat_id, site, callback_data.days_ago, user, session, settings)


@router.callback_query(ReportView.filter())
async def on_report_view(
    call: CallbackQuery, callback_data: ReportView, bot: Bot, user: User, session: AsyncSession
) -> None:
    """Сохранённый отчёт объекта за день — по кнопке под вечерней сводкой, без запроса к LLM."""
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    work_date = date.fromordinal(callback_data.day)
    saved = await ReportRepo(session).get(site.id, work_date) if site else None
    if site is None or saved is None:
        await call.answer("Отчёт не найден", show_alert=True)
        return
    entries = await EntryRepo(session).for_report(site.id, work_date)
    text = render_report(
        SiteDailyReport.model_validate(saved.data),
        site.name,
        work_date,
        Counter(e.kind for e in entries),
    )
    await call.answer()
    chat_id = call.message.chat.id if call.message else call.from_user.id
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        markup = feedback_keyboard(site.id, work_date) if last else None
        await bot.send_message(chat_id, chunk, reply_markup=markup)
