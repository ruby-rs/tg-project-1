import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.db.models import Entry, EntryStatus
from app.db.repositories import EntryRepo
from app.worker.processor import EntryProcessor

log = logging.getLogger(__name__)


def retry_delay(attempt: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** (attempt - 1), 900))


class Worker:
    """Очередь задач поверх таблицы entries (SELECT ... FOR UPDATE SKIP LOCKED).

    Без Redis/брокера: задачи переживают рестарт, можно поднять несколько воркеров.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        processor: EntryProcessor,
        *,
        concurrency: int = 4,
        poll_interval: float = 2.0,
        max_attempts: int = 5,
        stale_after: int = 600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._processor = processor
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._stale_after = stale_after

    async def claim(self, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            ids = await EntryRepo(session).claim_batch(limit, self._stale_after)
            await session.commit()
            return ids

    async def run(self, stop: asyncio.Event) -> None:
        log.info("Воркер запущен, параллельность %d", self._concurrency)
        tasks: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            free = self._concurrency - len(tasks)
            ids: list[int] = []
            if free > 0:
                try:
                    ids = await self.claim(free)
                except Exception:
                    log.exception("Ошибка при получении задач")
            for entry_id in ids:
                task = asyncio.create_task(self.handle(entry_id))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            if free <= 0:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            elif not ids:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass

        if tasks:
            log.info("Дожидаюсь %d задач перед остановкой", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

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
                await self._mark_failed_attempt(session, entry_id, exc)
                return

        await self._processor.notify_done(entry)

    async def _load(self, session: AsyncSession, entry_id: int) -> Entry | None:
        return await session.get(
            Entry, entry_id, options=[joinedload(Entry.company)], populate_existing=True
        )

    async def _mark_failed_attempt(
        self, session: AsyncSession, entry_id: int, exc: Exception
    ) -> None:
        entry = await self._load(session, entry_id)
        if entry is None:
            return
        entry.error = f"{type(exc).__name__}: {exc}"[:2000]
        entry.locked_at = None
        final = entry.attempts >= self._max_attempts
        if final:
            entry.status = EntryStatus.FAILED
        else:
            entry.status = EntryStatus.PENDING
            entry.next_attempt_at = datetime.now(UTC) + retry_delay(entry.attempts)
        await session.commit()
        if final:
            await self._processor.notify_failed(entry)
