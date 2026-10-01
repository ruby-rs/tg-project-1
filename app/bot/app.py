import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import get_routers
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.config import Settings
from app.db.session import create_engine, create_sessionmaker

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="object", description="Выбрать объект"),
    BotCommand(command="report", description="Отчёт за день"),
    BotCommand(command="new_object", description="Добавить объект"),
    BotCommand(command="invite", description="Пригласить прораба"),
    BotCommand(command="help", description="Как пользоваться"),
]


def build_dispatcher(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> Dispatcher:
    # FSM в памяти: для MVP достаточно, при масштабировании — RedisStorage
    dp = Dispatcher(storage=MemoryStorage(), settings=settings)
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
    dp = build_dispatcher(settings, sessionmaker)

    try:
        await bot.set_my_commands(COMMANDS)
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()
