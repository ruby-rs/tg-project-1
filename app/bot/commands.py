"""Меню команд по ролям: у прораба — только его команды, у руководителя — полное."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User, UserRole

log = logging.getLogger(__name__)

FOREMAN_COMMANDS = [
    BotCommand(command="report", description="Отчёт за день"),
    BotCommand(command="object", description="Выбрать объект"),
    BotCommand(command="archive", description="Архив объекта"),
    BotCommand(command="new_object", description="Добавить объект"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="privacy", description="Персональные данные"),
]

MANAGER_COMMANDS = [
    BotCommand(command="report", description="Отчёт по объекту"),
    BotCommand(command="stats", description="Статистика за неделю"),
    BotCommand(command="archive", description="Архив объекта"),
    BotCommand(command="sites", description="Объекты"),
    BotCommand(command="team", description="Команда"),
    BotCommand(command="invite", description="Пригласить прораба"),
    BotCommand(command="settings", description="Сводка и напоминания"),
    BotCommand(command="new_object", description="Добавить объект"),
    BotCommand(command="object", description="Выбрать свой объект"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="privacy", description="Персональные данные"),
]


async def set_default_commands(bot: Bot) -> None:
    await bot.set_my_commands(FOREMAN_COMMANDS)


async def sync_user_commands(bot: Bot, user: User) -> None:
    """Руководителю — расширенное меню в его чате, остальным — общее (как у прораба)."""
    scope = BotCommandScopeChat(chat_id=user.tg_id)
    try:
        if user.company is not None and user.is_manager:
            await bot.set_my_commands(MANAGER_COMMANDS, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramAPIError:
        log.warning("Не удалось обновить меню команд пользователя %s", user.id)


async def sync_all_managers(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """При старте бота: меню руководителей могло не обновиться (например, после миграции)."""
    async with sessionmaker() as session:
        managers = await session.scalars(
            select(User).where(
                User.company_id.is_not(None),
                User.role.in_([UserRole.OWNER, UserRole.MANAGER]),
            )
        )
        for user in managers:
            await sync_user_commands(bot, user)
