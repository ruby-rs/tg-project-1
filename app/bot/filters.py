from aiogram.filters import Filter
from aiogram.types import TelegramObject

from app.db.models import User


class HasCompany(Filter):
    """Пользователь уже состоит в компании."""

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.company_id is not None


class IsManager(Filter):
    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.company_id is not None and user.is_manager
