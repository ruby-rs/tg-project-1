from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.commands import MANAGER_COMMANDS
from app.db.models import User

router = Router(name="fallback")

MANAGER_ONLY = {c.command for c in MANAGER_COMMANDS} - {"help", "privacy"}


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, user: User | None = None) -> None:
    command = message.text.split(maxsplit=1)[0].removeprefix("/").split("@", 1)[0].lower()
    if user is None or user.company_id is None:
        await message.answer("Сначала зарегистрируйтесь: /start")
    elif command in MANAGER_ONLY:
        await message.answer(
            f"/{command} доступна только руководителю. "
            "Назначить руководителем может владелец компании в /team."
        )
    else:
        await message.answer("Не знаю такой команды. Список команд: /help")


@router.message()
async def anything_else(message: Message, state: FSMContext, user: User | None = None) -> None:
    if user is None or user.company_id is None:
        await message.answer("Сначала зарегистрируйтесь: /start")
    elif await state.get_state() is not None:
        await message.answer("Жду ответ на предыдущий вопрос. Отменить — /cancel")
    else:
        await message.answer("Такой тип сообщений я пока не сохраняю.")
