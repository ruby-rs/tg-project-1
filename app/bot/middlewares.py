from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import UserRepo

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия БД на апдейт; коммит, если хендлер отработал без исключения."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._sessionmaker() as session:
            data["session"] = session
            result = await handler(event, data)
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Подгружает (или создаёт) пользователя вместе с компанией и текущим объектом."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        session: AsyncSession = data["session"]
        data["user"] = await UserRepo(session).get_or_create(
            tg_user.id, tg_user.full_name, tg_user.username
        )
        return await handler(event, data)
