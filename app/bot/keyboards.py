from collections.abc import Sequence
from datetime import date

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.db.models import Site, User


class Menu:
    """Кнопки главного меню под полем ввода — всё основное без команд."""

    REPORT = "📋 Отчёт"
    SITE = "📍 Объект"
    ARCHIVE = "🗂 Архив"
    HELP = "❓ Помощь"
    STATS = "📊 Статистика"
    SITES = "🏗 Объекты"
    TEAM = "👥 Команда"
    INVITE = "🔗 Пригласить"
    SETTINGS = "⚙️ Настройки"

    FOREMAN = (REPORT, SITE, ARCHIVE, HELP)
    MANAGER_ONLY = (STATS, SITES, TEAM, INVITE, SETTINGS)
    ALL = FOREMAN + MANAGER_ONLY


def main_menu(user: User) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    if user.is_manager:
        for text in (
            Menu.REPORT,
            Menu.STATS,
            Menu.SITES,
            Menu.TEAM,
            Menu.INVITE,
            Menu.SETTINGS,
            Menu.ARCHIVE,
            Menu.SITE,
            Menu.HELP,
        ):
            kb.button(text=text)
        kb.adjust(2, 2, 2, 3)
    else:
        for text in Menu.FOREMAN:
            kb.button(text=text)
        kb.adjust(2, 2)
    return kb.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Фото, голосовое или текст с объекта",
    )


class SiteAdmin(CallbackData, prefix="sa"):
    action: str  # list | card | rename | address | invite | members | kick | close | open
    site_id: int = 0
    user_id: int = 0


class TeamAdmin(CallbackData, prefix="ta"):
    action: str  # list | card | promote | demote | remove | remove_ok | invite
    user_id: int = 0


class ConsentAction(CallbackData, prefix="consent"):
    action: str  # accept | revoke | privacy


class Cancel(CallbackData, prefix="cancel"):
    """Отмена текущего ввода (вместо /cancel)."""


def cancel_keyboard(text: str = "✖️ Отмена") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=Cancel())
    return kb.as_markup()


class SiteSelect(CallbackData, prefix="site"):
    site_id: int


class NewSite(CallbackData, prefix="newsite"):
    pass


class ReportSite(CallbackData, prefix="rep"):
    site_id: int
    days_ago: int = 0


class InviteLink(CallbackData, prefix="invite"):
    site_id: int  # 0 — приглашение в компанию без объекта


def invite_keyboard(sites: Sequence[Site]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        kb.button(text=f"🏗 {site.name}", callback_data=InviteLink(site_id=site.id))
    kb.button(text="🏢 В компанию, без объекта", callback_data=InviteLink(site_id=0))
    kb.adjust(1)
    return kb.as_markup()


def sites_keyboard(
    sites: Sequence[Site], current_id: int | None, *, can_create: bool = False
) -> InlineKeyboardMarkup:
    """Выбор объекта. Кнопка «Новый объект» — только руководителю (can_create)."""
    kb = InlineKeyboardBuilder()
    for site in sites:
        mark = "✅ " if site.id == current_id else ""
        kb.button(text=f"{mark}{site.name}", callback_data=SiteSelect(site_id=site.id))
    if can_create:
        kb.button(text="➕ Новый объект", callback_data=NewSite())
    kb.adjust(1)
    return kb.as_markup()


class ReportMenu(CallbackData, prefix="rm"):
    """Выбор отчёта кнопками: site_id=0 — список объектов, иначе — выбор дня."""

    site_id: int = 0


REPORT_DAYS = [(0, "Сегодня"), (1, "Вчера"), (2, "Позавчера")]


def report_days_keyboard(site_id: int, *, can_switch: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for days_ago, title in REPORT_DAYS:
        kb.button(text=title, callback_data=ReportSite(site_id=site_id, days_ago=days_ago))
    if can_switch:
        kb.button(text="🏗 Другой объект", callback_data=ReportMenu())
    kb.adjust(3, 1)
    return kb.as_markup()


def report_pick_site_keyboard(sites: Sequence[Site]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        kb.button(text=f"🏗 {site.name}", callback_data=ReportMenu(site_id=site.id))
    kb.adjust(1)
    return kb.as_markup()


def report_sites_keyboard(sites: Sequence[Site], days_ago: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        kb.button(text=site.name, callback_data=ReportSite(site_id=site.id, days_ago=days_ago))
    kb.adjust(1)
    return kb.as_markup()


class ReportView(CallbackData, prefix="rv"):
    """Показать сохранённый отчёт объекта за день (кнопки под вечерней сводкой)."""

    site_id: int
    day: int  # date.toordinal()


def digest_keyboard(sites: Sequence[tuple[int, str]], work_date: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site_id, name in sites:
        kb.button(
            text=f"📋 {name}",
            callback_data=ReportView(site_id=site_id, day=work_date.toordinal()),
        )
    kb.adjust(1)
    return kb.as_markup()


class Feedback(CallbackData, prefix="fb"):
    site_id: int
    day: int  # date.toordinal()
    rating: int  # 1 | -1


class ReportRebuild(CallbackData, prefix="rb"):
    site_id: int
    day: int  # date.toordinal()


class ReportMedia(CallbackData, prefix="rmed"):
    """Прислать фото и видео за день, которые прислали прорабы."""

    site_id: int
    day: int  # date.toordinal()


def feedback_keyboard(site_id: int, work_date: date, media_count: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    day = work_date.toordinal()
    kb.button(text="👍 Полезно", callback_data=Feedback(site_id=site_id, day=day, rating=1))
    kb.button(text="👎 Есть ошибки", callback_data=Feedback(site_id=site_id, day=day, rating=-1))
    kb.button(text="🔄 Пересобрать", callback_data=ReportRebuild(site_id=site_id, day=day))
    if media_count:
        kb.button(
            text=f"📸 Медиа за день ({media_count})",
            callback_data=ReportMedia(site_id=site_id, day=day),
        )
    kb.adjust(2, 1, 1)
    return kb.as_markup()
