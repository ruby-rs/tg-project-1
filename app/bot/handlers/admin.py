"""Управление объектами (/sites) и командой (/team) — для руководителей."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsManager
from app.bot.handlers.start import SITE_INVITE_PREFIX
from app.bot.states import SiteEdit
from app.db.models import Site, User, UserRole
from app.db.repositories import SiteRepo, UserRepo

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(IsManager())
router.callback_query.filter(IsManager())

ROLE_TITLES = {
    UserRole.OWNER: "владелец",
    UserRole.MANAGER: "руководитель",
    UserRole.FOREMAN: "прораб",
}
ROLE_ICONS = {UserRole.OWNER: "👑", UserRole.MANAGER: "🧑‍💼", UserRole.FOREMAN: "👷"}


class SiteAdmin(CallbackData, prefix="sa"):
    action: str  # list | card | rename | address | invite | members | kick | close | open
    site_id: int = 0
    user_id: int = 0


class TeamAdmin(CallbackData, prefix="ta"):
    action: str  # list | card | promote | demote | remove | remove_ok
    user_id: int = 0


async def _notify(bot: Bot, user: User, text: str) -> None:
    try:
        await bot.send_message(user.tg_id, text)
    except Exception:
        log.info("Не удалось уведомить пользователя %s", user.id)


async def _edit_or_answer(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await call.answer()
    if call.message:
        await call.message.edit_text(text, reply_markup=markup)


# ---------- Объекты ----------


def _sites_list_markup(sites: list[Site]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for site in sites:
        icon = "🏗" if site.is_active else "🗄"
        suffix = "" if site.is_active else " (закрыт)"
        kb.button(
            text=f"{icon} {site.name}{suffix}",
            callback_data=SiteAdmin(action="card", site_id=site.id),
        )
    kb.adjust(1)
    return kb.as_markup()


async def _site_card(session: AsyncSession, site: Site) -> tuple[str, InlineKeyboardMarkup]:
    members = await SiteRepo(session).members(site.id)
    foremen = [m for m in members if m.role == UserRole.FOREMAN]
    lines = [
        f"🏗 <b>{escape(site.name)}</b>",
        f"📍 Адрес: {escape(site.address) if site.address else 'не указан'}",
        f"Статус: {'работает' if site.is_active else '🗄 закрыт'}",
        "👷 Прорабы: "
        + (", ".join(escape(m.full_name) for m in foremen) if foremen else "пока никого"),
    ]
    kb = InlineKeyboardBuilder()
    sid = site.id
    kb.button(text="✏️ Название", callback_data=SiteAdmin(action="rename", site_id=sid))
    kb.button(text="📍 Адрес", callback_data=SiteAdmin(action="address", site_id=sid))
    if site.is_active:
        kb.button(
            text="🔗 Ссылка для прораба", callback_data=SiteAdmin(action="invite", site_id=sid)
        )
    if foremen:
        kb.button(text="👷 Убрать прораба", callback_data=SiteAdmin(action="members", site_id=sid))
    if site.is_active:
        kb.button(text="🗄 Закрыть объект", callback_data=SiteAdmin(action="close", site_id=sid))
    else:
        kb.button(text="♻️ Открыть снова", callback_data=SiteAdmin(action="open", site_id=sid))
    kb.button(text="← Все объекты", callback_data=SiteAdmin(action="list"))
    kb.adjust(2, 1, 1, 1, 1)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("sites"))
async def cmd_sites(message: Message, user: User, session: AsyncSession) -> None:
    sites = await SiteRepo(session).list_all(user.company_id)
    if not sites:
        await message.answer("Объектов пока нет. Добавьте первый: /new_object")
        return
    await message.answer("Управление объектами:", reply_markup=_sites_list_markup(sites))


@router.callback_query(SiteAdmin.filter(F.action == "list"))
async def on_sites_list(call: CallbackQuery, user: User, session: AsyncSession) -> None:
    sites = await SiteRepo(session).list_all(user.company_id)
    await _edit_or_answer(call, "Управление объектами:", _sites_list_markup(sites))


@router.callback_query(SiteAdmin.filter())
async def on_site_action(
    call: CallbackQuery,
    callback_data: SiteAdmin,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    repo = SiteRepo(session)
    site = await repo.get_in_company(user.company_id, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    action = callback_data.action

    if action in ("rename", "address"):
        await state.set_state(SiteEdit.name if action == "rename" else SiteEdit.address)
        await state.update_data(site_id=site.id)
        await call.answer()
        prompt = (
            "Введите новое название объекта:"
            if action == "rename"
            else "Введите адрес объекта (или «-», чтобы удалить):"
        )
        if call.message:
            await call.message.answer(prompt + "\nОтменить — /cancel")
        return

    if action == "invite":
        link = await create_start_link(bot, SITE_INVITE_PREFIX + site.invite_code)
        await call.answer()
        if call.message:
            await call.message.answer(
                f"Ссылка на объект «{escape(site.name)}» — перешлите её прорабу:\n\n{link}"
            )
        return

    if action == "members":
        foremen = [m for m in await repo.members(site.id) if m.role == UserRole.FOREMAN]
        kb = InlineKeyboardBuilder()
        for m in foremen:
            kb.button(
                text=f"❌ {m.full_name}",
                callback_data=SiteAdmin(action="kick", site_id=site.id, user_id=m.id),
            )
        kb.button(text="← Назад", callback_data=SiteAdmin(action="card", site_id=site.id))
        kb.adjust(1)
        await _edit_or_answer(call, f"Кого убрать с объекта «{escape(site.name)}»?", kb.as_markup())
        return

    if action == "kick":
        target = await UserRepo(session).get_in_company(user.company_id, callback_data.user_id)
        if target is not None:
            await repo.remove_member(site.id, target)
            await _notify(bot, target, f"Вас убрали с объекта «{escape(site.name)}».")
    elif action == "close":
        await repo.set_active(site, False)
    elif action == "open":
        await repo.set_active(site, True)

    text, markup = await _site_card(session, site)
    await _edit_or_answer(call, text, markup)


@router.message(SiteEdit.name, F.text & ~F.text.startswith("/"))
async def on_site_rename(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 255:
        await message.answer("Название должно быть от 2 до 255 символов. Попробуйте ещё раз.")
        return
    repo = SiteRepo(session)
    site = await repo.get_in_company(user.company_id, (await state.get_data()).get("site_id", 0))
    if site is None:
        await state.clear()
        await message.answer("Объект не найден.")
        return
    duplicate = await repo.get_by_name(user.company_id, name)
    if duplicate is not None and duplicate.id != site.id:
        await message.answer("Объект с таким названием уже есть. Введите другое.")
        return
    site.name = name
    await state.clear()
    text, markup = await _site_card(session, site)
    await message.answer("✅ Название изменено.\n\n" + text, reply_markup=markup)


@router.message(SiteEdit.address, F.text & ~F.text.startswith("/"))
async def on_site_address(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    address = message.text.strip()
    site = await SiteRepo(session).get_in_company(
        user.company_id, (await state.get_data()).get("site_id", 0)
    )
    await state.clear()
    if site is None:
        await message.answer("Объект не найден.")
        return
    site.address = None if address in ("-", "—") else address[:500]
    text, markup = await _site_card(session, site)
    await message.answer("✅ Адрес сохранён.\n\n" + text, reply_markup=markup)


# ---------- Команда ----------


def _team_list_markup(users: list[User]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        kb.button(
            text=f"{ROLE_ICONS.get(u.role, '')} {u.full_name} — {ROLE_TITLES.get(u.role, u.role)}",
            callback_data=TeamAdmin(action="card", user_id=u.id),
        )
    kb.adjust(1)
    return kb.as_markup()


def _can_manage(actor: User, target: User) -> bool:
    """Владелец управляет всеми, кроме себя; руководитель — только прорабами."""
    if target.id == actor.id or target.role == UserRole.OWNER:
        return False
    return actor.role == UserRole.OWNER or target.role == UserRole.FOREMAN


async def _member_card(
    session: AsyncSession, actor: User, target: User
) -> tuple[str, InlineKeyboardMarkup]:
    sites = await SiteRepo(session).list_for_user(target)
    lines = [
        f"{ROLE_ICONS.get(target.role, '')} <b>{escape(target.full_name)}</b>",
        f"Роль: {ROLE_TITLES.get(target.role, target.role)}",
    ]
    if target.username:
        lines.append(f"Telegram: @{escape(target.username)}")
    if target.role == UserRole.FOREMAN:
        lines.append("Объекты: " + (", ".join(escape(s.name) for s in sites) if sites else "нет"))
    kb = InlineKeyboardBuilder()
    if _can_manage(actor, target) and actor.role == UserRole.OWNER:
        if target.role == UserRole.FOREMAN:
            kb.button(
                text="🧑‍💼 Сделать руководителем",
                callback_data=TeamAdmin(action="promote", user_id=target.id),
            )
        else:
            kb.button(
                text="👷 Сделать прорабом",
                callback_data=TeamAdmin(action="demote", user_id=target.id),
            )
    if _can_manage(actor, target):
        kb.button(
            text="🚫 Удалить из компании",
            callback_data=TeamAdmin(action="remove", user_id=target.id),
        )
    kb.button(text="← Команда", callback_data=TeamAdmin(action="list"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("team"))
async def cmd_team(message: Message, user: User, session: AsyncSession) -> None:
    users = await UserRepo(session).list_company(user.company_id)
    await message.answer(
        "Команда компании. Пригласить прораба — /invite", reply_markup=_team_list_markup(users)
    )


@router.callback_query(TeamAdmin.filter(F.action == "list"))
async def on_team_list(call: CallbackQuery, user: User, session: AsyncSession) -> None:
    users = await UserRepo(session).list_company(user.company_id)
    await _edit_or_answer(
        call, "Команда компании. Пригласить прораба — /invite", _team_list_markup(users)
    )


@router.callback_query(TeamAdmin.filter())
async def on_team_action(
    call: CallbackQuery, callback_data: TeamAdmin, bot: Bot, user: User, session: AsyncSession
) -> None:
    users = UserRepo(session)
    target = await users.get_in_company(user.company_id, callback_data.user_id)
    if target is None:
        await call.answer("Сотрудник не найден", show_alert=True)
        return
    action = callback_data.action
    company_name = escape(user.company.name)

    if action != "card" and not _can_manage(user, target):
        await call.answer("Недостаточно прав", show_alert=True)
        return

    if action in ("promote", "demote"):
        if user.role != UserRole.OWNER:
            await call.answer("Менять роли может только владелец", show_alert=True)
            return
        target.role = UserRole.MANAGER if action == "promote" else UserRole.FOREMAN
        await _notify(
            bot,
            target,
            f"Ваша роль в компании «{company_name}»: {ROLE_TITLES[target.role]}.",
        )
    elif action == "remove":
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🚫 Да, удалить", callback_data=TeamAdmin(action="remove_ok", user_id=target.id)
        )
        kb.button(text="← Отмена", callback_data=TeamAdmin(action="card", user_id=target.id))
        kb.adjust(1)
        await _edit_or_answer(
            call,
            f"Удалить {escape(target.full_name)} из компании? Его сообщения останутся в "
            "архиве и отчётах, но присылать новые он не сможет.",
            kb.as_markup(),
        )
        return
    elif action == "remove_ok":
        name = escape(target.full_name)
        await users.remove_from_company(target)
        await _notify(bot, target, f"Вас удалили из компании «{company_name}».")
        remaining = await users.list_company(user.company_id)
        await _edit_or_answer(call, f"✅ {name} удалён из компании.", _team_list_markup(remaining))
        return

    text, markup = await _member_card(session, user, target)
    await _edit_or_answer(call, text, markup)
