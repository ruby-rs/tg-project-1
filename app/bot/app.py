import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.commands import set_default_commands, sync_all_managers
from app.bot.handlers import get_routers
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.config import Settings
from app.db.session import create_engine, create_sessionmaker

log = logging.getLogger(__name__)


def build_storage(settings: Settings) -> BaseStorage:
    """Состояния диалогов (регистрация, создание объекта) — в Redis, чтобы рестарт
    бота их не сбрасывал. Без REDIS_URL — в памяти."""
    if not settings.redis_url:
        return MemoryStorage()
    from aiogram.fsm.storage.redis import RedisStorage

    week = 7 * 24 * 3600
    return RedisStorage.from_url(settings.redis_url, state_ttl=week, data_ttl=week)


def build_dispatcher(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: BaseStorage | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=storage or MemoryStorage(), settings=settings)
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.update.outer_middleware(UserMiddleware())
    dp.include_routers(*get_routers())
    return dp


async def run_bot(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(settings, sessionmaker, build_storage(settings))

    try:
        await set_default_commands(bot)
        await sync_all_managers(bot, sessionmaker)
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await dp.storage.close()
        await bot.session.close()
        await engine.dispose()
