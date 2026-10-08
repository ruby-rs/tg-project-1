import logging
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.enums import ChatAction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.bot.keyboards import feedback_keyboard
from app.db.models import EntryStatus, ReportJob, Site
from app.db.repositories import EntryRepo, ReportJobRepo
from app.reports.render import split_message
from app.services.media import collect_media, send_media_collection
from app.services.reports import ReportService
from app.worker.runner import mark_failed_attempt

log = logging.getLogger(__name__)

# Сколько отчёт ждёт, пока воркер расшифрует голосовые за этот день
WAIT_FOR_ENTRIES = timedelta(minutes=10)
WAIT_POLL = timedelta(seconds=15)


class ReportQueue:
    """Сборка отчётов через LLM. Запрос может идти минуты — поэтому не в процессе бота."""

    name = "reports"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        report_service: ReportService,
        *,
        max_attempts: int = 3,
        # Запрос к медленной локальной LLM с повтором может идти ~30 минут
        stale_after: int = 3600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bot = bot
        self._reports = report_service
        self._max_attempts = max_attempts
        self._stale_after = stale_after

    async def claim(self, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            ids = await ReportJobRepo(session).claim_batch(limit, self._stale_after)
            await session.commit()
            return ids

    async def _load(self, session: AsyncSession, job_id: int) -> ReportJob | None:
        return await session.get(
            ReportJob,
            job_id,
            options=[joinedload(ReportJob.site).joinedload(Site.company)],
            populate_existing=True,
        )

    async def handle(self, job_id: int) -> None:
        async with self._sessionmaker() as session:
            job = await self._load(session, job_id)
            if job is None:
                return
            site = job.site

            # Голосовые за этот день ещё расшифровываются — подождём, чтобы они вошли в отчёт
            unprocessed = await EntryRepo(session).count_unprocessed(site.id, job.work_date)
            if unprocessed and datetime.now(UTC) - job.created_at < WAIT_FOR_ENTRIES:
                job.status = EntryStatus.PENDING
                job.attempts -= 1  # ожидание — не неудачная попытка
                job.locked_at = None
                job.next_attempt_at = datetime.now(UTC) + WAIT_POLL
                await session.commit()
                return

            await self._typing(job.chat_id)
            try:
                result = await self._reports.build(
                    session, site, job.work_date, site.company.timezone, reuse=not job.rebuild
                )
                job.status = EntryStatus.DONE
                job.error = None
                job.locked_at = None
                job.finished_at = datetime.now(UTC)
                await session.commit()
                entries = await EntryRepo(session).for_report(site.id, job.work_date)
            except Exception as exc:
                log.exception("Ошибка отчёта job=%s (попытка %s)", job_id, job.attempts)
                await session.rollback()
                job = await self._load(session, job_id)
                if job is None:
                    return
                final = mark_failed_attempt(job, exc, self._max_attempts)
                await session.commit()
                if final:
                    await self._send(
                        job.chat_id,
                        f"❌ Не удалось сформировать отчёт по «{escape(job.site.name)}» "
                        f"за {job.work_date:%d.%m.%Y}. Попробуйте позже.",
                    )
                return

        if result is None:
            text = f"За {job.work_date:%d.%m.%Y} по объекту «{escape(site.name)}» сообщений нет."
            await self._send(job.chat_id, text)
            return
        media_count = collect_media(entries).total
        chunks = split_message(result.render(site.name, job.work_date))
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            markup = feedback_keyboard(site.id, job.work_date, media_count) if last else None
            await self._send(job.chat_id, chunk, markup)
        # Сборник медиа — к запрошенному отчёту; при пересборке он уже есть в чате
        # (и доступен по кнопке «📸 Медиа за день»)
        if media_count and not job.rebuild:
            try:
                await send_media_collection(
                    self._bot, job.chat_id, entries, site.name, job.work_date, site.company.timezone
                )
            except Exception:
                log.exception("Не удалось прислать медиа к отчёту job=%s", job_id)

    async def _typing(self, chat_id: int) -> None:
        try:
            await self._bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            log.debug("Не удалось отправить «печатает» в чат %s", chat_id)

    async def _send(self, chat_id: int, text: str, markup=None) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:
            log.exception("Не удалось отправить отчёт в чат %s", chat_id)
