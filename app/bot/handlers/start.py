from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.commands import sync_user_commands
from app.bot.filters import IsManager
from app.bot.handlers.sites import no_sites_text, set_current_site
from app.bot.keyboards import (
    ConsentAction,
    InviteLink,
    Menu,
    Nav,
    NewSite,
    home_text,
    invite_keyboard,
    main_menu,
    sites_keyboard,
    with_menu,
)
from app.bot.screens import PROMPT_KEY, drop_reply_keyboard, reply_to_input, show
from app.bot.states import Registration
from app.config import Settings
from app.db.models import Company, User, UserRole
from app.db.repositories import CompanyRepo, SiteRepo

router = Router(name="start")

INVITE_PREFIX = "inv_"  # приглашение в компанию
SITE_INVITE_PREFIX = "site_"  # приглашение сразу на объект
BAD_INVITE = "Ссылка-приглашение недействительна. Попросите у руководителя новую."

_HELP_INTRO = """\
<b>Как пользоваться</b>

Команда одна — /menu, дальше всё кнопками.

1. «📍 Объект» — выберите объект, все сообщения будут привязаны к нему.
2. В течение дня присылайте сюда всё с объекта: фото, видео, голосовые, текст.
   Голосовые я расшифрую и пришлю текст ответом.
3. «📋 Отчёт» — выберите день, и я соберу отчёт. Вместе с отчётом пришлю
   альбомом фото и видео за этот день. Под отчётом: 👎 — написать, что не так,
   и я пересоберу отчёт с учётом замечания; 🔄 — просто собрать заново;
   📸 — ещё раз прислать фото и видео.
4. «🗂 Архив» — выгрузить архив объекта за период одним ZIP-файлом.

Чтобы сохранить исходное фото с датой съёмки (для споров с заказчиком),
отправляйте его «файлом», а не как сжатое фото.
"""

FOREMAN_HELP = (
    _HELP_INTRO
    + """
Новый объект добавляет руководитель — попросите у него ссылку на объект."""
)

MANAGER_HELP = (
    _HELP_INTRO
    + """
<b>Для руководителя</b>
«📊 Статистика» — расшифровки, оценки отчётов, активность прорабов за неделю
«🏗 Объекты» — добавить объект, название, адрес, ссылка для прораба, закрыть объект
«👥 Команда» — роли и состав команды
«🔗 Пригласить» — ссылка-приглашение для прораба
«⚙️ Настройки» — время вечерней сводки и напоминаний, рабочие дни, часовой пояс"""
)


def help_text(user: User) -> str:
    return MANAGER_HELP if user.is_manager else FOREMAN_HELP


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await process_start(bot, message.chat.id, command.args or "", user, session, state)


async def _join_company(bot: Bot, chat_id: int, user: User, company: Company) -> bool:
    """Подключает пользователя к компании прорабом. False — он уже в другой компании."""
    if user.company_id is not None and user.company_id != company.id:
        await bot.send_message(
            chat_id,
            f"Вы уже состоите в компании «{escape(user.company.name)}». "
            "Перейти в другую компанию пока можно только через поддержку.",
        )
        return False
    if user.company_id is None:
        user.company = company
        user.role = UserRole.FOREMAN
    return True


async def process_start(
    bot: Bot, chat_id: int, args: str, user: User, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    sites = SiteRepo(session)

    if args.startswith(SITE_INVITE_PREFIX):
        site = await sites.get_by_invite(args.removeprefix(SITE_INVITE_PREFIX))
        if site is None:
            await bot.send_message(chat_id, BAD_INVITE)
            return
        if not await _join_company(bot, chat_id, user, site.company):
            return
        await session.flush()
        await sites.add_member(site.id, user.id)
        text = await set_current_site(session, user, site)
        await drop_reply_keyboard(bot, chat_id)
        await bot.send_message(
            chat_id,
            f"👷 Вы подключены к компании «{escape(site.company.name)}».\n{text}\n\n"
            "Присылайте сюда фото, голосовые и текст с объекта. Меню всегда можно "
            "открыть командой /menu.",
            reply_markup=main_menu(user),
        )
        return

    if args.startswith(INVITE_PREFIX):
        company = await CompanyRepo(session).get_by_invite(args.removeprefix(INVITE_PREFIX))
        if company is None:
            await bot.send_message(chat_id, BAD_INVITE)
            return
        if not await _join_company(bot, chat_id, user, company):
            return
        await session.flush()
        my_sites = await sites.list_for_user(user)
        text = f"👷 Вы подключены к компании «{escape(company.name)}»."
        await drop_reply_keyboard(bot, chat_id)
        if my_sites:
            await bot.send_message(
                chat_id,
                f"{text}\n\nВыберите объект, на котором вы сегодня работаете:",
                reply_markup=with_menu(
                    sites_keyboard(my_sites, user.current_site_id, can_create=user.is_manager)
                ),
            )
        else:
            await bot.send_message(
                chat_id, f"{text}\n\n{no_sites_text(user)}", reply_markup=main_menu(user)
            )
        return

    if user.company_id is not None:
        await drop_reply_keyboard(bot, chat_id)
        await bot.send_message(chat_id, home_text(user), reply_markup=main_menu(user))
        return

    await state.set_state(Registration.company_name)
    await drop_reply_keyboard(bot, chat_id)
    sent = await bot.send_message(
        chat_id,
        "Здравствуйте! Я собираю отчёты прорабов с объектов: фото, голосовые и текст "
        "превращаю в структурированный дневной отчёт.\n\n"
        "Если вы <b>руководитель</b> — напишите название компании, и я её зарегистрирую.\n"
        "Если вы <b>прораб</b> — попросите у руководителя ссылку-приглашение.",
    )
    await state.update_data({PROMPT_KEY: sent.message_id})


@router.message(
    Registration.company_name, F.text & ~F.text.startswith("/") & ~F.text.in_(Menu.LEGACY)
)
async def register_company(
    message: Message,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 255:
        await message.answer("Название должно быть от 2 до 255 символов. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    company = await CompanyRepo(session).create(name, settings.default_timezone, user)
    await state.clear()
    await sync_user_commands(bot, user)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить первый объект", callback_data=NewSite())
    await reply_to_input(
        message,
        data,
        f"🏗 Компания «{escape(company.name)}» создана, вы — руководитель.\n\n"
        "Дальше: добавьте объекты и отправьте прорабам ссылку из «🔗 Пригласить». "
        "Вы тоже можете присылать сюда фото и голосовые с объектов.\n"
        "Меню всегда открывается командой /menu.",
        with_menu(kb.as_markup()),
    )


async def invite_screen(user: User, session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    sites = await SiteRepo(session).list_for_user(user)
    return (
        "🔗 Куда пригласить прораба? Ссылка на объект сразу даёт доступ к нему; "
        "уже подключённому прорабу её можно прислать, чтобы добавить ещё объект.",
        with_menu(invite_keyboard(sites)),
    )


@router.message(Command("invite"), IsManager())
async def cmd_invite(message: Message, user: User, session: AsyncSession) -> None:
    text, markup = await invite_screen(user, session)
    await message.answer(text, reply_markup=markup)


@router.callback_query(InviteLink.filter(), IsManager())
async def on_invite_link(
    call: CallbackQuery, callback_data: InviteLink, bot: Bot, user: User, session: AsyncSession
) -> None:
    if callback_data.site_id == 0:
        payload, target = INVITE_PREFIX + user.company.invite_code, "в компанию"
    else:
        site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
        if site is None:
            await call.answer("Объект не найден", show_alert=True)
            return
        payload, target = SITE_INVITE_PREFIX + site.invite_code, f"на объект «{escape(site.name)}»"
    link = await create_start_link(bot, payload)
    kb = InlineKeyboardBuilder()
    kb.button(text="← Другая ссылка", callback_data=Nav(to="invite"))
    await show(
        call,
        f"🔗 Ссылка-приглашение {target} — перешлите её прорабу:\n\n{link}",
        with_menu(kb.as_markup()),
    )


def help_screen(user: User) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Персональные данные", callback_data=ConsentAction(action="privacy"))
    return help_text(user), with_menu(kb.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    text, markup = help_screen(user)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    if user.company_id is None:
        await message.answer("Отменено.")
    else:
        await message.answer(home_text(user), reply_markup=main_menu(user))
