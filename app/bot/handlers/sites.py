from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.keyboards import NewSite, SiteSelect, sites_keyboard
from app.bot.states import SiteCreation
from app.db.models import Site, User
from app.db.repositories import EntryRepo, SiteRepo

router = Router(name="sites")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())


async def _set_current_site(session: AsyncSession, user: User, site: Site) -> str:
    user.current_site = site
    moved = await EntryRepo(session).assign_unsorted(user.id, site.id)
    text = f"📍 Текущий объект: <b>{escape(site.name)}</b>\nВсе новые сообщения — сюда."
    if moved:
        text += f"\nПривязал к нему ранее присланные сообщения: {moved}."
    return text


@router.message(Command("object", "objects"))
async def cmd_object(message: Message, user: User, session: AsyncSession) -> None:
    sites = await SiteRepo(session).list_active(user.company_id)
    if not sites:
        await message.answer("Объектов пока нет. Добавьте первый: /new_object")
        return
    await message.answer(
        "Выберите объект:", reply_markup=sites_keyboard(sites, user.current_site_id)
    )


@router.callback_query(SiteSelect.filter())
async def on_site_selected(
    call: CallbackQuery, callback_data: SiteSelect, user: User, session: AsyncSession
) -> None:
    site = await SiteRepo(session).get(user.company_id, callback_data.site_id)
    if site is None:
        await call.answer("Объект не найден", show_alert=True)
        return
    text = await _set_current_site(session, user, site)
    await call.answer()
    if call.message:
        await call.message.edit_text(text)


@router.message(Command("new_object"))
async def cmd_new_object(message: Message, state: FSMContext) -> None:
    await state.set_state(SiteCreation.name)
    await message.answer("Как называется объект? Например: «ЖК Северный, корпус 2».")


@router.callback_query(NewSite.filter())
async def on_new_site(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SiteCreation.name)
    await call.answer()
    if call.message:
        await call.message.answer("Как называется объект? Например: «ЖК Северный, корпус 2».")


@router.message(SiteCreation.name, F.text & ~F.text.startswith("/"))
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
            "Такой объект уже есть. Выберите его через /object или введите другое имя."
        )
        return
    site = await repo.create(user.company_id, name)
    await state.clear()
    await message.answer("🏗 Объект добавлен.\n" + await _set_current_site(session, user, site))
