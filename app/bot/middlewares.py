import time
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import ConsentAction
from app.config import Settings
from app.db.models import User
from app.db.repositories import UserRepo
from app.services.service_state import ServiceStateCache

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


PAUSED_TEXT = (
    "⏸ Бот временно остановлен администратором. Сообщения сейчас не сохраняются — "
    "пришлите их, когда бот снова заработает."
)
NOTICE_INTERVAL = 60.0  # сек: не повторять предупреждение на каждое фото альбома


def company_disabled_text(company_name: str, support_contact: str | None) -> str:
    text = (
        f"⛔ Работа компании «{escape(company_name)}» в боте приостановлена. "
        "Сообщения сейчас не сохраняются."
    )
    if support_contact:
        text += f"\nПо вопросам: {escape(support_contact)}"
    return text


def _is_privacy(event: TelegramObject) -> bool:
    """Отозвать согласие на обработку данных (152-ФЗ) можно всегда."""
    if isinstance(event, Message):
        return (event.text or "").split("@", 1)[0].strip() == "/privacy"
    if isinstance(event, CallbackQuery) and event.data:
        try:
            return ConsentAction.unpack(event.data).action in ("privacy", "revoke")
        except (TypeError, ValueError):
            return False
    return False


class AccessGateMiddleware(BaseMiddleware):
    """Остановка бота и отключённые компании: такие апдейты не доходят до хендлеров.

    Главных администраторов не касается — иначе бота некому было бы запустить.
    """

    def __init__(self, state: ServiceStateCache, settings: Settings) -> None:
        self._state = state
        self._superadmins = settings.superadmins
        self._support = settings.support_contact
        self._notified: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.id in self._superadmins or _is_privacy(event):
            return await handler(event, data)
        if await self._state.paused():
            await self._deny(event, tg_user.id, PAUSED_TEXT)
            return None
        user: User | None = data.get("user")
        if user is not None and user.company is not None and user.company.disabled_at:
            await self._deny(
                event, tg_user.id, company_disabled_text(user.company.name, self._support)
            )
            return None
        return await handler(event, data)

    async def _deny(self, event: TelegramObject, tg_id: int, text: str) -> None:
        if isinstance(event, CallbackQuery):
            # Всплывающее окно не больше 200 символов
            await event.answer(text.split("\n", 1)[0][:200], show_alert=True)
            return
        if not isinstance(event, Message) or event.edit_date is not None:
            return  # правки сообщений отклоняем молча
        now = time.monotonic()
        if now - self._notified.get(tg_id, -NOTICE_INTERVAL) < NOTICE_INTERVAL:
            return
        self._notified[tg_id] = now
        await event.answer(text)
