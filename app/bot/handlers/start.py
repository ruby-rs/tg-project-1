from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.deep_linking import create_start_link
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.commands import sync_user_commands
from app.bot.filters import IsManager
from app.bot.handlers.sites import set_current_site
from app.bot.keyboards import InviteLink, invite_keyboard, sites_keyboard
from app.bot.states import Registration
from app.config import Settings
from app.db.models import Company, User, UserRole
from app.db.repositories import CompanyRepo, SiteRepo

router = Router(name="start")

INVITE_PREFIX = "inv_"  # приглашение в компанию
SITE_INVITE_PREFIX = "site_"  # приглашение сразу на объект
BAD_INVITE = "Ссылка-приглашение недействительна. Попросите у руководителя новую."

HELP_TEXT = """\
<b>Как пользоваться</b>

1. Выберите объект: /object — все сообщения будут привязаны к нему.
2. В течение дня присылайте сюда всё с объекта: фото, голосовые, текст.
   Голосовые я расшифрую и пришлю текст ответом.
3. /report — отчёт по текущему объекту за сегодня, /report вчера — за вчера.

Чтобы сохранить исходное фото с датой съёмки (для споров с заказчиком),
отправляйте его «файлом», а не как сжатое фото.

<b>Команды</b>
/object — выбрать объект
/new_object — добавить объект
/report — отчёт за день
/archive — выгрузить архив фото и сообщений объекта
/invite — пригласить прораба на объект (для руководителя)
/sites — управление объектами (для руководителя)
/team — команда и роли (для руководителя)
/settings — время сводки и напоминаний, часовой пояс (для руководителя)
/stats — статистика за неделю (для руководителя)
/privacy — персональные данные и отзыв согласия
/cancel — отменить текущее действие"""


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
        await bot.send_message(
            chat_id, f"👷 Вы подключены к компании «{escape(site.company.name)}».\n{text}"
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
        text = f"👷 Вы подключены к компании «{escape(company.name)}».\n\n"
        if my_sites:
            await bot.send_message(
                chat_id,
                text + "Выберите объект, на котором вы сегодня работаете:",
                reply_markup=sites_keyboard(my_sites, user.current_site_id),
            )
        else:
            await bot.send_message(
                chat_id,
                text + "Вас пока не добавили ни на один объект. Попросите у руководителя "
                "ссылку на объект или добавьте свой: /new_object",
            )
        return

    if user.company_id is not None:
        await bot.send_message(chat_id, HELP_TEXT)
        return

    await state.set_state(Registration.company_name)
    await bot.send_message(
        chat_id,
        "Здравствуйте! Я собираю отчёты прорабов с объектов: фото, голосовые и текст "
        "превращаю в структурированный дневной отчёт.\n\n"
        "Если вы <b>руководитель</b> — напишите название компании, и я её зарегистрирую.\n"
        "Если вы <b>прораб</b> — попросите у руководителя ссылку-приглашение.",
    )


@router.message(Registration.company_name, F.text & ~F.text.startswith("/"))
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
    company = await CompanyRepo(session).create(name, settings.default_timezone, user)
    await state.clear()
    await sync_user_commands(bot, user)
    await message.answer(
        f"🏗 Компания «{escape(company.name)}» создана, вы — руководитель.\n\n"
        "Дальше:\n"
        "1. /new_object — добавьте объекты\n"
        "2. /invite — отправьте ссылку прорабам\n\n"
        "Вы тоже можете присылать сюда фото и голосовые с объектов."
    )


@router.message(Command("invite"), IsManager())
async def cmd_invite(message: Message, user: User, session: AsyncSession) -> None:
    sites = await SiteRepo(session).list_for_user(user)
    await message.answer(
        "Куда пригласить прораба? Ссылка на объект сразу даёт доступ к нему; "
        "уже подключённому прорабу её можно прислать, чтобы добавить ещё объект.",
        reply_markup=invite_keyboard(sites),
    )


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
    await call.answer()
    if call.message:
        await call.message.answer(f"Ссылка-приглашение {target} — перешлите её прорабу:\n\n{link}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")
