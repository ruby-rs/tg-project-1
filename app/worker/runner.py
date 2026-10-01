import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.db.models import Entry, EntryStatus, ReportJob
from app.db.repositories import EntryRepo
from app.worker.processor import EntryProcessor

log = logging.getLogger(__name__)


def retry_delay(attempt: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** (attempt - 1), 900))


def mark_failed_attempt(job: Entry | ReportJob, exc: Exception, max_attempts: int) -> bool:
    """Ставит задачу на повтор с бэкоффом. True — попытки кончились, задача провалена."""
    job.error = f"{type(exc).__name__}: {exc}"[:2000]
    job.locked_at = None
    if job.attempts >= max_attempts:
        job.status = EntryStatus.FAILED
        return True
    job.status = EntryStatus.PENDING
    job.next_attempt_at = datetime.now(UTC) + retry_delay(job.attempts)
    return False


class JobQueue(Protocol):
    name: str

    async def claim(self, limit: int) -> list[int]: ...

    async def handle(self, job_id: int) -> None: ...


async def run_queue(
    queue: JobQueue, stop: asyncio.Event, *, concurrency: int, poll_interval: float
) -> None:
    """Очередь поверх таблицы БД (SELECT ... FOR UPDATE SKIP LOCKED).

    Без Redis/брокера: задачи переживают рестарт, можно поднять несколько воркеров.
    """
    log.info("Очередь %s запущена, параллельность %d", queue.name, concurrency)
    tasks: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        free = concurrency - len(tasks)
        ids: list[int] = []
        if free > 0:
            try:
                ids = await queue.claim(free)
            except Exception:
                log.exception("Ошибка при получении задач из очереди %s", queue.name)
        for job_id in ids:
            task = asyncio.create_task(queue.handle(job_id))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if free <= 0:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        elif not ids:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    if tasks:
        log.info("Очередь %s: дожидаюсь %d задач перед остановкой", queue.name, len(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)


class EntryQueue:
    """Сообщения прорабов: скачать файл в архив, расшифровать, описать фото."""

    name = "entries"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        processor: EntryProcessor,
        *,
        max_attempts: int = 5,
        stale_after: int = 600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._processor = processor
        self._max_attempts = max_attempts
        self._stale_after = stale_after

    async def claim(self, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            ids = await EntryRepo(session).claim_batch(limit, self._stale_after)
            await session.commit()
            return ids

    async def handle(self, entry_id: int) -> None:
        async with self._sessionmaker() as session:
            entry = await self._load(session, entry_id)
            if entry is None:
                return
            try:
                await self._processor.process(session, entry)
                entry.status = EntryStatus.DONE
                entry.error = None
                entry.locked_at = None
                await session.commit()
            except Exception as exc:
                log.exception("Ошибка обработки entry=%s (попытка %s)", entry_id, entry.attempts)
                await session.rollback()
                entry = await self._load(session, entry_id)
                if entry is None:
                    return
                final = mark_failed_attempt(entry, exc, self._max_attempts)
                await session.commit()
                if final:
                    await self._processor.notify_failed(entry)
                return

            message_id = await self._processor.notify_done(entry)
            if message_id is not None:
                entry.transcript_message_id = message_id
                await session.commit()

    async def _load(self, session: AsyncSession, entry_id: int) -> Entry | None:
        return await session.get(
            Entry, entry_id, options=[joinedload(Entry.company)], populate_existing=True
        )
