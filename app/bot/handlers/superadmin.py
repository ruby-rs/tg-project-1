"""Панель главного администратора сервиса (/admin).

Главные администраторы задаются в SUPERADMIN_IDS и не зависят от компаний: видят
активность объектов всех компаний, отключают и включают компании и объекты,
останавливают и запускают весь бот. Остальным /admin не отвечает — команда
проходит дальше, как неизвестная.
"""

import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.types import User as TgUser
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.screens import show
from app.config import Settings
from app.db.models import Company, User, UserRole
from app.db.repositories import (
    INACTIVE_DAYS,
    CompanyActivity,
    PlatformRepo,
    ServiceStateRepo,
    SiteActivity,
    SiteRepo,
    UserRepo,
)
from app.services.service_state import ServiceStateCache
from app.timeutils import to_local

log = logging.getLogger(__name__)

router = Router(name="superadmin")

PAGE_SIZE = 8
MAX_SITE_LINES = 30
MAX_IDLE_LINES = 40


class IsSuperAdmin(Filter):
    async def __call__(
        self,
        event: TelegramObject,
        event_from_user: TgUser | None = None,
        settings: Settings | None = None,
    ) -> bool:
        return (
            event_from_user is not None
            and settings is not None
            and event_from_user.id in settings.superadmins
        )


router.message.filter(IsSuperAdmin())
router.callback_query.filter(IsSuperAdmin())


class Sup(CallbackData, prefix="sup"):
    # home | companies | company | site | idle | co_off | co_off_ok | co_on
    # | site_off | site_on | stop | stop_ok | start
    action: str
    id: int = 0
    page: int = 0


# ---------- Статусы активности ----------


def site_status(a: SiteActivity, company: Company) -> tuple[str, str]:
    """Значок и пояснение: работает ли объект и когда с него присылали сообщения."""
    if company.disabled_at is not None:
        return "⛔", "компания отключена"
    if not a.site.is_active:
        return "🗄", "закрыт"
    idle = a.idle_days
    if idle is None:
        return "⚪", "сообщений ещё не было"
    if idle <= 0:
        return "🟢", f"сегодня {a.today_count}, за неделю {a.week_count}"
    last = a.last_date.strftime("%d.%m")
    if idle < INACTIVE_DAYS:
        return "🟡", f"последнее {last}, за неделю {a.week_count}"
    return "🔴", f"тишина {idle} дн. (последнее {last})"


def company_icon(c: CompanyActivity) -> str:
    if c.company.disabled_at is not None:
        return "⛔"
    open_sites = c.open_sites
    if not open_sites:
        return "⚪"
    idle = [s.idle_days for s in open_sites if s.idle_days is not None]
    if any(d <= 0 for d in idle):
        return "🟢"
    if any(d < INACTIVE_DAYS for d in idle):
        return "🟡"
    return "🔴"


def _local(dt: datetime, cfg: Settings) -> str:
    return to_local(dt, cfg.default_timezone).strftime("%d.%m.%Y %H:%M")


# ---------- Экраны ----------


def _row(kb: InlineKeyboardBuilder, *buttons: tuple[str, Sup]) -> None:
    kb.row(*(InlineKeyboardButton(text=text, callback_data=data.pack()) for text, data in buttons))


async def home_screen(session: AsyncSession, cfg: Settings) -> tuple[str, InlineKeyboardMarkup]:
    platform = PlatformRepo(session)
    state = await ServiceStateRepo(session).get()
    companies = await platform.activity(datetime.now(UTC), cfg.work_day_start_hour)
    queues = await platform.queues()

    enabled = [c for c in companies if c.company.disabled_at is None]
    open_sites = [s for c in enabled for s in c.open_sites]
    today = sum(1 for s in open_sites if s.idle_days is not None and s.idle_days <= 0)
    inactive = sum(1 for s in open_sites if s.is_inactive)
    recent = len(open_sites) - today - inactive

    if state.paused_at is None:
        bot_line = "Бот: 🟢 работает"
    else:
        bot_line = f"Бот: ⏸ <b>остановлен</b> с {_local(state.paused_at, cfg)}"
    lines = [
        "🛠 <b>Панель администратора</b>",
        "",
        bot_line,
        f"🏢 Компаний: {len(companies)}, отключено: {len(companies) - len(enabled)}",
        f"🏗 Объектов в работе: {len(open_sites)}",
        f"    🟢 сегодня {today} · 🟡 1–{INACTIVE_DAYS - 1} дн. назад {recent} · "
        f"🔴 неактивны {inactive}",
        f"✉️ Сообщений сегодня: {sum(s.today_count for s in open_sites)}, "
        f"за 7 дней: {sum(s.week_count for s in open_sites)}",
        f"⚙️ Ждут обработки: {queues.entries_waiting} · "
        f"ошибок за неделю: {queues.entries_failed_week} · "
        f"отчётов в очереди: {queues.reports_waiting}",
        "",
        f"🔴 — объект открыт, но сообщений нет {INACTIVE_DAYS} дня и больше.",
    ]
    kb = InlineKeyboardBuilder()
    kb.button(text="🏢 Компании", callback_data=Sup(action="companies"))
    kb.button(text=f"🔴 Неактивные ({inactive})", callback_data=Sup(action="idle"))
    if state.paused_at is None:
        kb.button(text="⏸ Остановить бота", callback_data=Sup(action="stop"))
    else:
        kb.button(text="▶️ Запустить бота", callback_data=Sup(action="start"))
    kb.button(text="🔄 Обновить", callback_data=Sup(action="home"))
    kb.adjust(2, 1, 1)
    return "\n".join(lines), kb.as_markup()


async def companies_screen(
    session: AsyncSession, cfg: Settings, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    companies = await PlatformRepo(session).activity(datetime.now(UTC), cfg.work_day_start_hour)
    pages = max(1, -(-len(companies) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    kb = InlineKeyboardBuilder()
    for c in companies[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        kb.button(
            text=f"{company_icon(c)} {c.company.name} · объектов {len(c.open_sites)}",
            callback_data=Sup(action="company", id=c.company.id),
        )
    kb.adjust(1)
    if pages > 1:
        nav = [("◀️", Sup(action="companies", page=page - 1))] if page > 0 else []
        nav.append((f"{page + 1} / {pages}", Sup(action="companies", page=page)))
        if page < pages - 1:
            nav.append(("▶️", Sup(action="companies", page=page + 1)))
        _row(kb, *nav)
    _row(kb, ("← Панель", Sup(action="home")))
    text = (
        "🏢 <b>Компании</b>\n\n"
        "🟢 есть сообщения сегодня · 🟡 были недавно · 🔴 все объекты молчат · "
        "⚪ нет открытых объектов · ⛔ отключена"
        if companies
        else "Компаний пока нет."
    )
    return text, kb.as_markup()


async def _owner(session: AsyncSession, company_id: int) -> User | None:
    return await session.scalar(
        select(User).where(User.company_id == company_id, User.role == UserRole.OWNER).limit(1)
    )


async def company_screen(
    session: AsyncSession, cfg: Settings, company_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    found = await PlatformRepo(session).activity(
        datetime.now(UTC), cfg.work_day_start_hour, company_id=company_id
    )
    if not found:
        return None
    c = found[0]
    company = c.company
    owner = await _owner(session, company.id)
    status = (
        "🟢 работает"
        if company.disabled_at is None
        else f"⛔ <b>отключена</b> с {_local(company.disabled_at, cfg)}"
    )
    lines = [
        f"🏢 <b>{escape(company.name)}</b>",
        f"Статус: {status}",
        f"Создана: {_local(company.created_at, cfg)[:10]} · пользователей: {c.users}",
    ]
    if owner is not None:
        contact = f" (@{escape(owner.username)})" if owner.username else ""
        lines.append(f"Владелец: {escape(owner.full_name)}{contact}")
    lines += ["", "<b>Объекты</b>"]
    if not c.sites:
        lines.append("Объектов пока нет.")
    for a in c.sites[:MAX_SITE_LINES]:
        icon, note = site_status(a, company)
        lines.append(f"{icon} {escape(a.site.name)} — {note}")
    if len(c.sites) > MAX_SITE_LINES:
        lines.append(f"…и ещё {len(c.sites) - MAX_SITE_LINES}")

    kb = InlineKeyboardBuilder()
    for a in c.sites[:MAX_SITE_LINES]:
        icon, _ = site_status(a, company)
        kb.button(text=f"{icon} {a.site.name}", callback_data=Sup(action="site", id=a.site.id))
    kb.adjust(1)
    if company.disabled_at is None:
        _row(kb, ("⛔ Отключить компанию", Sup(action="co_off", id=company.id)))
    else:
        _row(kb, ("✅ Включить компанию", Sup(action="co_on", id=company.id)))
    _row(kb, ("← Компании", Sup(action="companies")))
    return "\n".join(lines), kb.as_markup()


async def site_screen(
    session: AsyncSession, cfg: Settings, site_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    site = await PlatformRepo(session).get_site(site_id)
    if site is None:
        return None
    found = await PlatformRepo(session).activity(
        datetime.now(UTC), cfg.work_day_start_hour, company_id=site.company_id
    )
    a = next(s for s in found[0].sites if s.site.id == site_id)
    icon, note = site_status(a, site.company)
    lines = [
        f"🏗 <b>{escape(site.name)}</b>",
        f"🏢 {escape(site.company.name)}",
        f"📍 {escape(site.address) if site.address else 'адрес не указан'}",
        "",
        f"Статус: {'работает' if site.is_active else '🗄 закрыт'}",
        f"Активность: {icon} {note}",
    ]
    kb = InlineKeyboardBuilder()
    if site.is_active:
        kb.button(text="🗄 Закрыть объект", callback_data=Sup(action="site_off", id=site.id))
    else:
        kb.button(text="♻️ Открыть объект", callback_data=Sup(action="site_on", id=site.id))
    kb.button(text="← Компания", callback_data=Sup(action="company", id=site.company_id))
    kb.adjust(1)
    if site.company.disabled_at is not None:
        lines.append("\n⛔ Компания отключена — объект заработает, только когда её включат.")
    return "\n".join(lines), kb.as_markup()


async def idle_screen(session: AsyncSession, cfg: Settings) -> tuple[str, InlineKeyboardMarkup]:
    companies = await PlatformRepo(session).activity(datetime.now(UTC), cfg.work_day_start_hour)
    idle = [
        (a, c.company)
        for c in companies
        if c.company.disabled_at is None
        for a in c.open_sites
        if a.is_inactive
    ]
    # Сначала объекты, с которых так и не было сообщений, затем самые долгие паузы
    idle.sort(key=lambda x: -1 if x[0].idle_days is None else -x[0].idle_days)
    lines = [
        "🔴 <b>Неактивные объекты</b>",
        f"Открытые объекты, с которых нет сообщений {INACTIVE_DAYS} дня и больше.",
        "",
    ]
    if not idle:
        lines.append("Таких нет — все объекты на связи 👍")
    for a, company in idle[:MAX_IDLE_LINES]:
        icon, note = site_status(a, company)
        lines.append(f"{icon} {escape(a.site.name)} ({escape(company.name)}) — {note}")
    if len(idle) > MAX_IDLE_LINES:
        lines.append(f"…и ещё {len(idle) - MAX_IDLE_LINES}")
    kb = InlineKeyboardBuilder()
    kb.button(text="← Панель", callback_data=Sup(action="home"))
    return "\n".join(lines), kb.as_markup()


def _confirm(text: str, yes: str, yes_data: Sup, no_data: Sup) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text=yes, callback_data=yes_data)
    kb.button(text="✖️ Отмена", callback_data=no_data)
    kb.adjust(1)
    return text, kb.as_markup()


# ---------- Хендлеры ----------


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    text, markup = await home_screen(session, settings)
    await message.answer(text, reply_markup=markup)


async def _notify_managers(bot: Bot, session: AsyncSession, company_id: int, text: str) -> None:
    for manager in await UserRepo(session).managers_with_consent(company_id):
        try:
            await bot.send_message(manager.tg_id, text)
        except Exception:
            log.info("Не удалось уведомить руководителя %s", manager.id)


@router.callback_query(Sup.filter(F.action.in_({"stop", "stop_ok", "start"})))
async def on_bot_toggle(
    call: CallbackQuery,
    callback_data: Sup,
    session: AsyncSession,
    settings: Settings,
    service_state: ServiceStateCache,
) -> None:
    repo = ServiceStateRepo(session)
    if callback_data.action == "stop":
        await show(
            call,
            *_confirm(
                "⏸ <b>Остановить бота для всех?</b>\n\n"
                "• Пользователи получат ответ, что бот временно остановлен; присланное "
                "в это время не сохранится.\n"
                "• Воркер перестанет расшифровывать голосовые, собирать отчёты и рассылать "
                "сводки. Задачи из очереди продолжатся после запуска.\n"
                "• Главные администраторы продолжат пользоваться ботом — запустить его снова "
                "можно в этой панели.",
                "⏸ Да, остановить",
                Sup(action="stop_ok"),
                Sup(action="home"),
            ),
        )
        return
    if callback_data.action == "stop_ok":
        await repo.pause(call.from_user.id)
        service_state.set(True)
        log.warning("Бот остановлен администратором %s", call.from_user.id)
        notice = "Бот остановлен"
    else:
        await repo.resume()
        service_state.set(False)
        log.warning("Бот запущен администратором %s", call.from_user.id)
        notice = "Бот снова работает"
    await session.commit()
    await call.answer(notice)
    await show(call, *await home_screen(session, settings))


@router.callback_query(Sup.filter(F.action.in_({"co_off", "co_off_ok", "co_on"})))
async def on_company_toggle(
    call: CallbackQuery,
    callback_data: Sup,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    platform = PlatformRepo(session)
    company = await platform.get_company(callback_data.id)
    if company is None:
        await call.answer("Компания не найдена", show_alert=True)
        return
    name = escape(company.name)
    if callback_data.action == "co_off":
        await show(
            call,
            *_confirm(
                f"⛔ <b>Отключить компанию «{name}»?</b>\n\n"
                "Её руководители и прорабы не смогут пользоваться ботом, сводки и "
                "напоминания перестанут приходить. Все данные сохранятся — компанию можно "
                "включить обратно в любой момент.",
                "⛔ Да, отключить",
                Sup(action="co_off_ok", id=company.id),
                Sup(action="company", id=company.id),
            ),
        )
        return
    enable = callback_data.action == "co_on"
    if (company.disabled_at is None) != enable:
        await platform.set_company_enabled(company, enable)
        await session.commit()
        log.warning(
            "Компания %s %s администратором %s",
            company.id,
            "включена" if enable else "отключена",
            call.from_user.id,
        )
        await _notify_managers(
            bot,
            session,
            company.id,
            f"✅ Работа компании «{name}» в боте возобновлена."
            if enable
            else f"⛔ Администратор сервиса приостановил работу компании «{name}» в боте.",
        )
    await call.answer("Компания включена" if enable else "Компания отключена")
    screen = await company_screen(session, settings, company.id)
    if screen is not None:
        await show(call, *screen)


@router.callback_query(Sup.filter(F.action.in_({"site_off", "site_on"})))
async def on_site_toggle(
    call: CallbackQuery, callback_data: Sup, session: AsyncSession, settings: Settings
) -> None:
    site = await PlatformRepo(session).get_site(callback_data.id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    active = callback_data.action == "site_on"
    if site.is_active != active:
        await SiteRepo(session).set_active(site, active)
        await session.commit()
        log.warning(
            "Объект %s %s администратором %s",
            site.id,
            "открыт" if active else "закрыт",
            call.from_user.id,
        )
    await call.answer("Объект открыт" if active else "Объект закрыт")
    screen = await site_screen(session, settings, site.id)
    if screen is not None:
        await show(call, *screen)


@router.callback_query(Sup.filter())
async def on_nav(
    call: CallbackQuery, callback_data: Sup, session: AsyncSession, settings: Settings
) -> None:
    match callback_data.action:
        case "companies":
            screen = await companies_screen(session, settings, callback_data.page)
        case "company":
            screen = await company_screen(session, settings, callback_data.id)
        case "site":
            screen = await site_screen(session, settings, callback_data.id)
        case "idle":
            screen = await idle_screen(session, settings)
        case _:
            screen = await home_screen(session, settings)
    if screen is None:
        await call.answer("Не найдено — возможно, уже удалено", show_alert=True)
        return
    await show(call, *screen)
