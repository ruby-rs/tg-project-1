from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany, IsManager
from app.bot.keyboards import NewSite, SiteSelect, sites_keyboard
from app.bot.states import SiteCreation
from app.db.models import Site, User
from app.db.repositories import EntryRepo, SiteRepo

router = Router(name="sites")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())

NO_SITES_FOREMAN = (
    "Вас пока не добавили ни на один объект. Попросите у руководителя ссылку на объект."
)
NO_SITES_MANAGER = "Объектов пока нет. Добавьте первый: /new_object"


def no_sites_text(user: User) -> str:
    return NO_SITES_MANAGER if user.is_manager else NO_SITES_FOREMAN


async def set_current_site(session: AsyncSession, user: User, site: Site) -> str:
    user.current_site = site
    moved = await EntryRepo(session).assign_unsorted(user.id, site.id)
    text = f"📍 Текущий объект: <b>{escape(site.name)}</b>\nВсе новые сообщения — сюда."
    if moved:
        text += f"\nПривязал к нему ранее присланные сообщения: {moved}."
    return text


@router.message(Command("object", "objects"))
async def cmd_object(message: Message, user: User, session: AsyncSession) -> None:
    sites = await SiteRepo(session).list_for_user(user)
    if not sites:
        await message.answer(no_sites_text(user))
        return
    await message.answer(
        "Выберите объект:",
        reply_markup=sites_keyboard(sites, user.current_site_id, can_create=user.is_manager),
    )


@router.callback_query(SiteSelect.filter())
async def on_site_selected(
    call: CallbackQuery, callback_data: SiteSelect, user: User, session: AsyncSession
) -> None:
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    text = await set_current_site(session, user, site)
    await call.answer()
    if call.message:
        await call.message.edit_text(text)


# Объекты добавляет руководитель; прораб получает доступ по ссылке на объект
@router.message(Command("new_object"), IsManager())
async def cmd_new_object(message: Message, state: FSMContext) -> None:
    await state.set_state(SiteCreation.name)
    await message.answer("Как называется объект? Например: «ЖК Северный, корпус 2».")


@router.callback_query(NewSite.filter(), IsManager())
async def on_new_site(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SiteCreation.name)
    await call.answer()
    if call.message:
        await call.message.answer("Как называется объект? Например: «ЖК Северный, корпус 2».")


@router.callback_query(NewSite.filter())
async def on_new_site_denied(call: CallbackQuery) -> None:
    """Кнопка из старого сообщения, нажатая прорабом."""
    await call.answer("Добавить объект может только руководитель.", show_alert=True)


@router.message(SiteCreation.name, F.text & ~F.text.startswith("/"), IsManager())
async def on_site_name(
    message: Message, user: User, session: AsyncSession, state: FSMContext
) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 255:
        await message.answer("Название должно быть от 2 до 255 символов. Попробуйте ещё раз.")
        return
    repo = SiteRepo(session)
    if await repo.get_by_name(user.company_id, name):
        await message.answer(
            "Такой объект уже есть в компании. Выберите его через /object, "
            "попросите у руководителя ссылку на него или введите другое имя."
        )
        return
    site = await repo.create(user.company_id, name, creator=user)
    await state.clear()
    await message.answer("🏗 Объект добавлен.\n" + await set_current_site(session, user, site))
