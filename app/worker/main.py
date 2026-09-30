import asyncio
import logging
import signal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings
from app.db.session import create_engine, create_sessionmaker
from app.services.llm import LLMClient
from app.services.photos import PhotoDescriber
from app.services.storage import LocalFileStorage
from app.services.transcription import build_transcriber
from app.worker.processor import EntryProcessor
from app.worker.runner import Worker

log = logging.getLogger(__name__)


async def run_worker(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    llm = LLMClient.from_settings(settings)
    processor = EntryProcessor(
        bot=bot,
        storage=LocalFileStorage(settings.media_root),
        transcriber=build_transcriber(settings),
        describer=PhotoDescriber(llm) if settings.llm_vision_model else None,
    )
    worker = Worker(
        sessionmaker,
        processor,
        concurrency=settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval,
        max_attempts=settings.worker_max_attempts,
        stale_after=settings.worker_stale_after,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await worker.run(stop)
    finally:
        await bot.session.close()
        await engine.dispose()
