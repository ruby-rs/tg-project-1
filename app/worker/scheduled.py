"""Плановые рассылки: вечерняя сводка руководителям и напоминания прорабам.

Планировщик раз в минуту смотрит на настройки компаний и создаёт задачи
в scheduled_runs; уникальный ключ (компания, вид, день) не даёт отправить
рассылку дважды, даже если воркеров несколько или воркер перезапустился.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.bot.keyboards import digest_keyboard
from app.db.models import (
    Company,
    EntryStatus,
    ScheduledKind,
    ScheduledRun,
    Site,
)
from app.db.repositories import EntryRepo, ScheduledRunRepo, UserRepo
from app.reports.digest import Digest, SiteDigest, render_digest
from app.reports.render import split_message
from app.services.reports import ReportService
from app.timeutils import work_date_for
from app.worker.runner import mark_failed_attempt

log = logging.getLogger(__name__)

# Если воркер лежал и поднялся позже, рассылка уйдёт, но не позже чем через это время
CATCH_UP_WINDOW = timedelta(hours=3)
WAIT_FOR_ENTRIES = timedelta(minutes=10)
WAIT_POLL = timedelta(seconds=30)

REMINDER_TEXT = (
    "📝 Сегодня от вас ещё не было сообщений по объекту «{site}». "
    "Пришлите фото и голосовое: что сделано за день, какие проблемы, что нужно завезти."
)


def due_kinds(company: Company, now_utc: datetime) -> list[str]:
    """Какие рассылки компании пора создать в этот момент."""
    local = now_utc.astimezone(ZoneInfo(company.timezone))
    workday = str(local.isoweekday()) in company.work_days
    due = []
    for kind, at in (
        (ScheduledKind.REMINDER, company.reminder_time),
        (ScheduledKind.DIGEST, company.digest_time),
    ):
        if at is None:
            continue
        # Напоминания — только в рабочие дни; сводка — всегда (в выходной уйдёт,
        # только если кто-то всё-таки работал)
        if kind == ScheduledKind.REMINDER and not workday:
            continue
        start = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if start <= local < start + CATCH_UP_WINDOW:
            due.append(kind)
    return due


async def schedule_due_runs(
    session: AsyncSession, now_utc: datetime, day_start_hour: int = 0
) -> int:
    created = 0
    runs = ScheduledRunRepo(session)
    for company in await session.scalars(select(Company)):
        today = work_date_for(now_utc, company.timezone, day_start_hour)
        for kind in due_kinds(company, now_utc):
            if await runs.create(company.id, kind, today):
                created += 1
                log.info("Рассылка %s для компании %s за %s", kind, company.id, today)
    return created


async def run_scheduler(
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval: float = 60,
    day_start_hour: int = 0,
) -> None:
    log.info("Планировщик рассылок запущен")
    while not stop.is_set():
        try:
            async with sessionmaker() as session:
                await schedule_due_runs(session, datetime.now(UTC), day_start_hour)
                await session.commit()
        except Exception:
            log.exception("Ошибка планировщика рассылок")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


class ScheduledQueue:
    name = "scheduled"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        report_service: ReportService,
        *,
        max_attempts: int = 3,
        stale_after: int = 3600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bot = bot
        self._reports = report_service
        self._max_attempts = max_attempts
        self._stale_after = stale_after

    async def claim(self, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            ids = await ScheduledRunRepo(session).claim_batch(limit, self._stale_after)
            await session.commit()
            return ids

    async def handle(self, run_id: int) -> None:
        async with self._sessionmaker() as session:
            run = await session.get(
                ScheduledRun, run_id, options=[joinedload(ScheduledRun.company)]
            )
            if run is None:
                return
            try:
                if run.kind == ScheduledKind.REMINDER:
                    await self._remind(session, run)
                elif not await self._digest(session, run):
                    return  # отложено: ждём расшифровку голосовых
                run.status = EntryStatus.DONE
                run.error = None
                run.locked_at = None
                run.finished_at = datetime.now(UTC)
                await session.commit()
            except Exception as exc:
                log.exception("Ошибка рассылки run=%s", run_id)
                await session.rollback()
                run = await session.get(ScheduledRun, run_id, populate_existing=True)
                if run is not None:
                    mark_failed_attempt(run, exc, self._max_attempts)
                    await session.commit()

    async def _remind(self, session: AsyncSession, run: ScheduledRun) -> None:
        for user, site in await UserRepo(session).foremen_to_remind(run.company_id, run.work_date):
            await self._send(user.tg_id, REMINDER_TEXT.format(site=escape(site.name)))

    async def _digest(self, session: AsyncSession, run: ScheduledRun) -> bool:
        """False — сводку отложили, потому что голосовые за день ещё расшифровываются."""
        company = run.company
        entries = EntryRepo(session)
        sites = list(
            await session.scalars(
                select(Site)
                .where(Site.company_id == company.id, Site.is_active.is_(True))
                .order_by(Site.name)
            )
        )

        young = datetime.now(UTC) - run.created_at < WAIT_FOR_ENTRIES
        for site in sites:
            if young and await entries.count_unprocessed(site.id, run.work_date):
                run.status = EntryStatus.PENDING
                run.attempts -= 1
                run.locked_at = None
                run.next_attempt_at = datetime.now(UTC) + WAIT_POLL
                await session.commit()
                return False

        digest = Digest(company_name=company.name, work_date=run.work_date)
        for site in sites:
            try:
                # Актуальные отчёты берутся из БД, LLM — только для изменившихся объектов
                result = await self._reports.build(session, site, run.work_date, company.timezone)
                await session.commit()
            except Exception:
                # LLM падает до записи в БД, откатывать нечего; сводка уйдёт с пометкой
                log.exception("Сводка: не удалось собрать отчёт по объекту %s", site.id)
                digest.failed.append(site.name)
                continue
            if result is None:
                digest.silent.append(site.name)
            else:
                digest.sites.append(
                    SiteDigest(site.id, site.name, sum(result.kinds.values()), result.report)
                )

        local = datetime.now(UTC).astimezone(ZoneInfo(company.timezone))
        workday = str(local.isoweekday()) in company.work_days
        if digest.is_empty and not workday:
            return True  # выходной, никто не работал — не беспокоим

        markup = digest_keyboard([(s.site_id, s.name) for s in digest.sites], run.work_date)
        chunks = split_message(render_digest(digest))
        for manager in await UserRepo(session).managers_with_consent(company.id):
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                await self._send(manager.tg_id, chunk, markup if last and digest.sites else None)
        return True

    async def _send(self, chat_id: int, text: str, markup=None) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:
            log.exception("Не удалось отправить рассылку в чат %s", chat_id)
