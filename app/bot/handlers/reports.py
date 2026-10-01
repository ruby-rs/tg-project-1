import logging
from datetime import timedelta
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.keyboards import ReportSite, report_sites_keyboard
from app.config import Settings
from app.db.models import Site, User
from app.db.repositories import EntryRepo, SiteRepo
from app.reports.render import split_message
from app.services.reports import ReportService
from app.timeutils import today_for

log = logging.getLogger(__name__)

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


async def _send_report(
    bot: Bot,
    chat_id: int,
    site: Site,
    days_ago: int,
    user: User,
    session: AsyncSession,
    settings: Settings,
    report_service: ReportService,
) -> None:
    tz = user.company.timezone
    work_date = today_for(tz, settings.work_day_start_hour) - timedelta(days=days_ago)
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            result = await report_service.build(session, site, work_date, tz)
    except Exception:
        log.exception("Ошибка формирования отчёта site=%s date=%s", site.id, work_date)
        await bot.send_message(
            chat_id, "❌ Не удалось сформировать отчёт. Попробуйте через минуту."
        )
        return

    if result is None:
        pending = await EntryRepo(session).count_unprocessed(site.id, work_date)
        text = f"За {work_date:%d.%m.%Y} по объекту «{escape(site.name)}» сообщений нет."
        if pending:
            text = (
                f"Сообщения за {work_date:%d.%m.%Y} ещё обрабатываются ({pending}). "
                "Повторите чуть позже."
            )
        await bot.send_message(chat_id, text)
        return

    for chunk in split_message(result.render(site.name, work_date)):
        await bot.send_message(chat_id, chunk)


@router.message(Command("report"))
async def cmd_report(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user: User,
    session: AsyncSession,
    settings: Settings,
    report_service: ReportService,
) -> None:
    days_ago = parse_days_ago(command.args)
    repo = SiteRepo(session)
    current = await repo.get_for_user(user, user.current_site_id) if user.current_site_id else None
    if current is not None and not user.is_manager:
        await _send_report(
            bot,
            message.chat.id,
            current,
            days_ago,
            user,
            session,
            settings,
            report_service,
        )
        return

    sites = await repo.list_for_user(user)
    if not sites:
        await message.answer("Объектов пока нет. Добавьте первый: /new_object")
    elif len(sites) == 1:
        await _send_report(
            bot, message.chat.id, sites[0], days_ago, user, session, settings, report_service
        )
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
    report_service: ReportService,
) -> None:
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    await call.answer(f"Формирую отчёт: {site.name}")
    chat_id = call.message.chat.id if call.message else call.from_user.id
    await _send_report(
        bot,
        chat_id,
        site,
        callback_data.days_ago,
        user,
        session,
        settings,
        report_service,
    )
