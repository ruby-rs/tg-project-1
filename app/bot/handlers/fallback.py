from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.commands import FOREMAN_COMMANDS, MANAGER_COMMANDS
from app.bot.keyboards import cancel_keyboard, main_menu
from app.db.models import User

router = Router(name="fallback")

MANAGER_ONLY = {c.command for c in MANAGER_COMMANDS} - {c.command for c in FOREMAN_COMMANDS}


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, user: User | None = None) -> None:
    command = message.text.split(maxsplit=1)[0].removeprefix("/").split("@", 1)[0].lower()
    if user is None or user.company_id is None:
        await message.answer("Сначала зарегистрируйтесь: /start")
    elif command in MANAGER_ONLY:
        await message.answer(
            f"/{command} доступна только руководителю. "
            "Назначить руководителем может владелец компании в «👥 Команда».",
            reply_markup=main_menu(user),
        )
    else:
        await message.answer(
            "Не знаю такой команды. Пользуйтесь кнопками меню внизу — «❓ Помощь» "
            "расскажет, что где.",
            reply_markup=main_menu(user),
        )


@router.message()
async def anything_else(message: Message, state: FSMContext, user: User | None = None) -> None:
    if user is None or user.company_id is None:
        await message.answer("Сначала зарегистрируйтесь: /start")
    elif await state.get_state() is not None:
        await message.answer(
            "Жду ответ на предыдущий вопрос — напишите его текстом.",
            reply_markup=cancel_keyboard(),
        )
    else:
        await message.answer("Такой тип сообщений я пока не сохраняю.")
