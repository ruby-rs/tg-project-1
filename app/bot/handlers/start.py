from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.deep_linking import create_start_link
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsManager
from app.bot.keyboards import sites_keyboard
from app.bot.states import Registration
from app.config import Settings
from app.db.models import User, UserRole
from app.db.repositories import CompanyRepo, SiteRepo

router = Router(name="start")

INVITE_PREFIX = "inv_"

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
/invite — ссылка-приглашение для прорабов (для руководителя)
/cancel — отменить текущее действие"""


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    args = command.args or ""

    if args.startswith(INVITE_PREFIX):
        company = await CompanyRepo(session).get_by_invite(args.removeprefix(INVITE_PREFIX))
        if company is None:
            await message.answer(
                "Ссылка-приглашение недействительна. Попросите у руководителя новую."
            )
            return
        if user.company_id is not None and user.company_id != company.id:
            await message.answer(
                f"Вы уже состоите в компании «{escape(user.company.name)}». "
                "Перейти в другую компанию пока можно только через поддержку."
            )
            return
        if user.company_id is None:
            user.company = company
            user.role = UserRole.FOREMAN
        sites = await SiteRepo(session).list_active(company.id)
        text = f"👷 Вы подключены к компании «{escape(company.name)}».\n\n"
        if sites:
            await message.answer(
                text + "Выберите объект, на котором вы сегодня работаете:",
                reply_markup=sites_keyboard(sites, user.current_site_id),
            )
        else:
            await message.answer(text + "Объектов пока нет — добавьте первый: /new_object")
        return

    if user.company_id is not None:
        await message.answer(HELP_TEXT)
        return

    await state.set_state(Registration.company_name)
    await message.answer(
        "Здравствуйте! Я собираю отчёты прорабов с объектов: фото, голосовые и текст "
        "превращаю в структурированный дневной отчёт.\n\n"
        "Если вы <b>руководитель</b> — напишите название компании, и я её зарегистрирую.\n"
        "Если вы <b>прораб</b> — попросите у руководителя ссылку-приглашение."
    )


@router.message(Registration.company_name, F.text & ~F.text.startswith("/"))
async def register_company(
    message: Message,
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
    await message.answer(
        f"🏗 Компания «{escape(company.name)}» создана, вы — руководитель.\n\n"
        "Дальше:\n"
        "1. /new_object — добавьте объекты\n"
        "2. /invite — отправьте ссылку прорабам\n\n"
        "Вы тоже можете присылать сюда фото и голосовые с объектов."
    )


@router.message(Command("invite"), IsManager())
async def cmd_invite(message: Message, user: User, bot: Bot) -> None:
    link = await create_start_link(bot, INVITE_PREFIX + user.company.invite_code)
    await message.answer(
        f"Перешлите эту ссылку прорабам — после перехода они попадут в вашу компанию:\n\n{link}"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")
