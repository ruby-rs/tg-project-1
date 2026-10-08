"""Список команд в Telegram: только /menu."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User, UserRole

log = logging.getLogger(__name__)

# Видна одна команда — меню; дальше всё кнопками. Остальные команды работают,
# но в списке команд Telegram не показываются.
MENU_COMMANDS = [BotCommand(command="menu", description="Открыть меню")]


async def set_default_commands(bot: Bot) -> None:
    await bot.set_my_commands(MENU_COMMANDS)


async def sync_user_commands(bot: Bot, user: User) -> None:
    """Убирает личный список команд (прежнее расширенное меню руководителя):
    у всех одинаковый список из одной команды, роль определяет кнопки меню."""
    scope = BotCommandScopeChat(chat_id=user.tg_id)
    try:
        await bot.delete_my_commands(scope=scope)
    except TelegramAPIError:
        log.warning("Не удалось обновить меню команд пользователя %s", user.id)


async def sync_all_managers(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """При старте бота: убрать личные списки команд, оставшиеся у руководителей."""
    async with sessionmaker() as session:
        managers = await session.scalars(
            select(User).where(
                User.company_id.is_not(None),
                User.role.in_([UserRole.OWNER, UserRole.MANAGER]),
            )
        )
        for user in managers:
            await sync_user_commands(bot, user)
