from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Site


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


def sites_keyboard(sites: Sequence[Site], current_id: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        mark = "✅ " if site.id == current_id else ""
        kb.button(text=f"{mark}{site.name}", callback_data=SiteSelect(site_id=site.id))
    kb.button(text="➕ Новый объект", callback_data=NewSite())
    kb.adjust(1)
    return kb.as_markup()


def report_sites_keyboard(sites: Sequence[Site], days_ago: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        kb.button(text=site.name, callback_data=ReportSite(site_id=site.id, days_ago=days_ago))
    kb.adjust(1)
    return kb.as_markup()
