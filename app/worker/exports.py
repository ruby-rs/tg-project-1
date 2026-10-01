import asyncio
import logging
import tempfile
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.db.models import EntryStatus, ExportJob, Site
from app.db.repositories import EntryRepo, ExportJobRepo
from app.services.archive import ArchiveTooLarge, build_archive
from app.services.storage import LocalFileStorage
from app.worker.runner import mark_failed_attempt

log = logging.getLogger(__name__)


def period_title(job: ExportJob) -> str:
    if job.date_from == job.date_to:
        return f"{job.date_from:%d.%m.%Y}"
    return f"{job.date_from:%d.%m.%Y}–{job.date_to:%d.%m.%Y}"


class ExportQueue:
    """Выгрузка архива объекта. Тяжёлая по диску, поэтому по одной за раз."""

    name = "exports"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        storage: LocalFileStorage,
        *,
        max_attempts: int = 2,
        stale_after: int = 3600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bot = bot
        self._storage = storage
        self._max_attempts = max_attempts
        self._stale_after = stale_after

    async def claim(self, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            ids = await ExportJobRepo(session).claim_batch(limit, self._stale_after)
            await session.commit()
            return ids

    async def handle(self, job_id: int) -> None:
        async with self._sessionmaker() as session:
            job = await session.get(
                ExportJob, job_id, options=[joinedload(ExportJob.site).joinedload(Site.company)]
            )
            if job is None:
                return
            try:
                await self._export(session, job)
                job.status = EntryStatus.DONE
                job.error = None
                job.locked_at = None
                job.finished_at = datetime.now(UTC)
                await session.commit()
            except Exception as exc:
                log.exception("Ошибка выгрузки job=%s", job_id)
                await session.rollback()
                job = await session.get(
                    ExportJob, job_id, options=[joinedload(ExportJob.site)], populate_existing=True
                )
                if job is None:
                    return
                final = mark_failed_attempt(job, exc, self._max_attempts)
                await session.commit()
                if final:
                    await self._send(
                        job.chat_id,
                        f"❌ Не удалось собрать архив по «{escape(job.site.name)}». "
                        "Попробуйте позже или выберите период короче.",
                    )

    async def _export(self, session: AsyncSession, job: ExportJob) -> None:
        site = job.site
        period = period_title(job)
        entries = await EntryRepo(session).for_export(site.id, job.date_from, job.date_to)
        if not entries:
            await self._send(job.chat_id, f"За {period} по «{escape(site.name)}» материалов нет.")
            job.files_count, job.total_size = 0, 0
            return

        await self._action(job.chat_id)
        base = f"archive_{site.id}_{job.date_from:%Y%m%d}-{job.date_to:%Y%m%d}"
        with tempfile.TemporaryDirectory(prefix="export_") as tmp:
            try:
                result = await asyncio.to_thread(
                    build_archive,
                    entries,
                    self._storage,
                    site.company.timezone,
                    Path(tmp),
                    base,
                    site.name,
                    period,
                )
            except ArchiveTooLarge as exc:
                mb = exc.total_size // (1024 * 1024)
                await self._send(
                    job.chat_id,
                    f"Архив за {period} получается слишком большим ({mb} МБ). "
                    "Выберите период короче.",
                )
                job.total_size = exc.total_size
                return

            job.files_count, job.total_size = result.files, result.total_size
            for n, path in enumerate(result.parts, start=1):
                caption = f"🗂 «{escape(site.name)}», {period}: файлов {result.files}"
                if len(result.parts) > 1:
                    caption += f" · часть {n} из {len(result.parts)}"
                await self._action(job.chat_id)
                # Ошибка отправки — повод для повтора задачи, поэтому не глушим её
                await self._bot.send_document(
                    job.chat_id, FSInputFile(path, filename=path.name), caption=caption
                )

    async def _action(self, chat_id: int) -> None:
        try:
            await self._bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        except Exception:
            log.debug("Не удалось отправить статус в чат %s", chat_id)

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не удалось отправить сообщение в чат %s", chat_id)
