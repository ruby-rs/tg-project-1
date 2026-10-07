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
from app.services.reports import ReportService
from app.services.storage import LocalFileStorage
from app.services.transcription import build_transcriber
from app.worker.exports import ExportQueue
from app.worker.processor import EntryProcessor
from app.worker.reports import ReportQueue
from app.worker.runner import EntryQueue, run_queue
from app.worker.scheduled import ScheduledQueue, run_scheduler

log = logging.getLogger(__name__)


async def run_worker(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    llm = LLMClient.from_settings(settings)
    storage = LocalFileStorage(settings.media_root)
    describer = PhotoDescriber(llm) if settings.llm_vision_model else None
    processor = EntryProcessor(
        bot=bot,
        storage=storage,
        transcriber=build_transcriber(settings),
        describer=describer,
    )
    entries = EntryQueue(
        sessionmaker,
        processor,
        max_attempts=settings.worker_max_attempts,
        stale_after=settings.worker_stale_after,
    )
    report_service = ReportService(llm, describer, storage)
    reports = ReportQueue(sessionmaker, bot, report_service)
    scheduled = ScheduledQueue(sessionmaker, bot, report_service)
    exports = ExportQueue(sessionmaker, bot, storage)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        # Отдельные очереди: долгий запрос к LLM не задерживает расшифровку голосовых
        await asyncio.gather(
            run_queue(
                entries,
                stop,
                concurrency=settings.worker_concurrency,
                poll_interval=settings.worker_poll_interval,
            ),
            run_queue(
                reports,
                stop,
                concurrency=settings.report_concurrency,
                poll_interval=settings.worker_poll_interval,
            ),
            # Вечерние сводки и напоминания: по одной рассылке за раз
            # Редкие очереди опрашиваем реже, чтобы не нагружать БД впустую
            run_queue(scheduled, stop, concurrency=1, poll_interval=10),
            # Выгрузка архива нагружает диск — по одной за раз
            run_queue(exports, stop, concurrency=1, poll_interval=5),
            run_scheduler(sessionmaker, stop, day_start_hour=settings.work_day_start_hour),
        )
    finally:
        await bot.session.close()
        await engine.dispose()
