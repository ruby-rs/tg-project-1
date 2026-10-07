import asyncio
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyReport, Entry, EntryKind, Site
from app.db.repositories import EntryRepo, FeedbackRepo, ReportRepo
from app.reports.prompts import report_system_prompt, report_user_prompt
from app.reports.render import render_report
from app.reports.schema import SiteDailyReport
from app.services.llm import LLMClient
from app.services.photos import PhotoDescriber
from app.services.storage import LocalFileStorage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReportResult:
    report: SiteDailyReport
    kinds: Counter[str]
    unprocessed: int

    def render(self, site_name: str, work_date: date) -> str:
        return render_report(self.report, site_name, work_date, self.kinds, self.unprocessed)


def is_fresh(saved: DailyReport | None, entries: Sequence[Entry]) -> bool:
    """Сохранённый отчёт актуален: с момента сборки не было новых и исправленных сообщений."""
    if saved is None or saved.entries_count != len(entries):
        return False
    last_change = max((e.edited_at or e.created_at) for e in entries)
    return last_change <= saved.updated_at


class ReportService:
    def __init__(
        self,
        llm: LLMClient,
        describer: PhotoDescriber | None = None,
        storage: LocalFileStorage | None = None,
    ) -> None:
        self._llm = llm
        self._describer = describer
        self._storage = storage

    async def _describe_missing_photos(self, entries: Sequence[Entry]) -> int:
        """Описывает фото, которые остались без описания при приёме: модель для фото
        тогда не была задана или не ответила (например, упёрлась в лимит).
        Возвращает, сколько фото удалось описать."""
        if self._describer is None or self._storage is None:
            return 0
        described = 0
        for entry in entries:
            if entry.kind != EntryKind.PHOTO or entry.photo_description or not entry.file_path:
                continue
            try:
                data = await asyncio.to_thread(self._storage.path(entry.file_path).read_bytes)
                entry.photo_description = await self._describer.describe(data, entry.text)
            except Exception:
                log.warning("Не удалось описать фото entry=%s перед отчётом", entry.id)
                continue
            described += 1
        return described

    async def build(
        self,
        session: AsyncSession,
        site: Site,
        work_date: date,
        tz_name: str,
        *,
        reuse: bool = True,
    ) -> ReportResult | None:
        """Отчёт по объекту за день. None — сообщений нет.

        Если с момента прошлой сборки не было новых и исправленных сообщений, возвращает
        сохранённый отчёт без запроса к LLM (reuse=False — собрать заново принудительно).
        """
        entries_repo = EntryRepo(session)
        entries = await entries_repo.for_report(site.id, work_date)
        unprocessed = await entries_repo.count_unprocessed(site.id, work_date)
        if not entries:
            return None
        kinds = Counter(e.kind for e in entries)
        # Новые описания фото меняют отчёт — сохранённый тогда не подходит
        if await self._describe_missing_photos(entries):
            reuse = False

        reports = ReportRepo(session)
        if reuse:
            saved = await reports.get(site.id, work_date)
            if is_fresh(saved, entries):
                try:
                    report = SiteDailyReport.model_validate(saved.data)
                    return ReportResult(report, kinds, unprocessed)
                except ValidationError:
                    log.warning("Сохранённый отчёт %s не прошёл проверку, пересобираю", saved.id)

        # Не держим транзакцию открытой на время запроса к LLM (это могут быть минуты):
        # данные для промпта уже прочитаны, ORM-объекты после commit остаются доступны
        corrections = await FeedbackRepo(session).comments(site.id, work_date)
        await session.commit()
        report = await self._llm.complete_json(
            report_system_prompt(),
            report_user_prompt(site, work_date, entries, tz_name, corrections),
            SiteDailyReport,
        )
        report.drop_unknown_entry_ids({e.id for e in entries})

        await reports.upsert(
            site.id, work_date, report.model_dump(mode="json"), self._llm.model, len(entries)
        )
        log.info("Отчёт: объект %s, %s, сообщений %d", site.id, work_date, len(entries))
        return ReportResult(report, kinds, unprocessed)
