import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Site
from app.db.repositories import EntryRepo, ReportRepo
from app.reports.prompts import report_system_prompt, report_user_prompt
from app.reports.render import render_report
from app.reports.schema import SiteDailyReport
from app.services.llm import LLMClient

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReportResult:
    report: SiteDailyReport
    kinds: Counter[str]
    unprocessed: int

    def render(self, site_name: str, work_date: date) -> str:
        return render_report(self.report, site_name, work_date, self.kinds, self.unprocessed)


class ReportService:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def build(
        self, session: AsyncSession, site: Site, work_date: date, tz_name: str
    ) -> ReportResult | None:
        """Собирает отчёт по объекту за день и сохраняет его. None — сообщений нет."""
        entries_repo = EntryRepo(session)
        entries = await entries_repo.for_report(site.id, work_date)
        unprocessed = await entries_repo.count_unprocessed(site.id, work_date)
        if not entries:
            return None

        report = await self._llm.complete_json(
            report_system_prompt(),
            report_user_prompt(site, work_date, entries, tz_name),
            SiteDailyReport,
        )
        report.drop_unknown_entry_ids({e.id for e in entries})

        await ReportRepo(session).upsert(
            site.id, work_date, report.model_dump(mode="json"), self._llm.model, len(entries)
        )
        log.info("Отчёт: объект %s, %s, сообщений %d", site.id, work_date, len(entries))
        return ReportResult(report, Counter(e.kind for e in entries), unprocessed)
