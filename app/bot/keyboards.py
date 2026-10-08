from collections.abc import Sequence
from datetime import date
from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Site, User


class Nav(CallbackData, prefix="m"):
    """Переход между разделами меню — в том же сообщении, без новых."""

    to: str  # home | report | site | archive | help | stats | sites | team | invite | settings


class Menu:
    """Подписи разделов главного меню."""

    REPORT = "📋 Отчёт"
    SITE = "📍 Объект"
    ARCHIVE = "🗂 Архив"
    HELP = "❓ Помощь"
    STATS = "📊 Статистика"
    SITES = "🏗 Объекты"
    TEAM = "👥 Команда"
    INVITE = "🔗 Пригласить"
    SETTINGS = "⚙️ Настройки"

    # Кнопки прежнего меню под полем ввода — у кого оно осталось, нажатие откроет новое
    LEGACY = (REPORT, SITE, ARCHIVE, HELP, STATS, SITES, TEAM, INVITE, SETTINGS)


_FOREMAN_ITEMS = [
    (Menu.REPORT, "report"),
    (Menu.SITE, "site"),
    (Menu.ARCHIVE, "archive"),
    (Menu.HELP, "help"),
]
_MANAGER_ITEMS = [
    (Menu.REPORT, "report"),
    (Menu.STATS, "stats"),
    (Menu.SITES, "sites"),
    (Menu.TEAM, "team"),
    (Menu.INVITE, "invite"),
    (Menu.SETTINGS, "settings"),
    (Menu.ARCHIVE, "archive"),
    (Menu.SITE, "site"),
    (Menu.HELP, "help"),
]
MANAGER_SECTIONS = frozenset({"stats", "sites", "team", "invite", "settings"})


def main_menu(user: User) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    items = _MANAGER_ITEMS if user.is_manager else _FOREMAN_ITEMS
    for text, to in items:
        kb.button(text=text, callback_data=Nav(to=to))
    if user.is_manager:
        kb.adjust(2, 2, 2, 3)
    else:
        kb.adjust(2, 2)
    return kb.as_markup()


def home_text(user: User) -> str:
    lines = ["☰ <b>Меню</b>"]
    if user.current_site is not None:
        lines.append(f"📍 Текущий объект: {escape(user.current_site.name)}")
    lines.append("\nФото, голосовые и текст с объекта просто присылайте в чат.")
    return "\n".join(lines)


BACK_TO_MENU = "← Меню"


def menu_button(kb: InlineKeyboardBuilder) -> InlineKeyboardBuilder:
    """Добавляет отдельной строкой кнопку возврата в главное меню."""
    kb.row(InlineKeyboardButton(text=BACK_TO_MENU, callback_data=Nav(to="home").pack()))
    return kb


def with_menu(markup: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder.from_markup(markup) if markup else InlineKeyboardBuilder()
    return menu_button(kb).as_markup()


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
    """Отмена текущего ввода: сообщение с вопросом снова становится меню
    (drop — удаляется, если вопрос задан отдельным сообщением, например под отчётом)."""

    drop: bool = False


def cancel_keyboard(text: str = "✖️ Отмена", *, drop: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=Cancel(drop=drop))
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


class Retranscribe(CallbackData, prefix="rt"):
    """Распознать голосовое заново, более точной моделью."""

    entry_id: int


def transcript_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Распознать заново", callback_data=Retranscribe(entry_id=entry_id))
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
