"""Вечерняя сводка руководителю: короткий блок по каждому объекту за день."""

from dataclasses import dataclass, field
from datetime import date
from html import escape

from app.reports.schema import Severity, SiteDailyReport

MAX_ALERTS_PER_SITE = 3


@dataclass(slots=True)
class SiteDigest:
    site_id: int
    name: str
    messages: int
    report: SiteDailyReport


@dataclass(slots=True)
class Digest:
    company_name: str
    work_date: date
    sites: list[SiteDigest] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)  # объекты без сообщений за день
    failed: list[str] = field(default_factory=list)  # отчёт собрать не удалось

    @property
    def is_empty(self) -> bool:
        return not self.sites and not self.failed


def _alerts(report: SiteDailyReport) -> list[str]:
    """Самое важное для руководителя: серьёзные проблемы и риски срыва сроков."""
    high = [i.description for i in report.issues if i.severity == Severity.HIGH]
    high += [r.description for r in report.schedule_risks if r.severity == Severity.HIGH]
    return high[:MAX_ALERTS_PER_SITE]


def render_digest(digest: Digest) -> str:
    e = escape
    total = len(digest.sites) + len(digest.silent) + len(digest.failed)
    out = [
        f"🗓 <b>Сводка за {digest.work_date:%d.%m.%Y}</b> · {e(digest.company_name)}",
        f"Объектов с сообщениями: {len(digest.sites) + len(digest.failed)} из {total}",
    ]
    for item in digest.sites:
        r = item.report
        counts = [f"✅ работ: {len(r.work_done)}"]
        if r.issues:
            counts.append(f"⚠️ проблем: {len(r.issues)}")
        if r.materials_needed:
            counts.append(f"📦 материалов: {len(r.materials_needed)}")
        if r.schedule_risks:
            counts.append(f"⏰ рисков: {len(r.schedule_risks)}")
        out += [
            "",
            f"🏗 <b>{e(item.name)}</b> — сообщений: {item.messages}",
            e(r.summary),
            " · ".join(counts),
            *(f"🔴 {e(a)}" for a in _alerts(r)),
        ]
    if digest.failed:
        out += ["", "❌ Не удалось собрать отчёт: " + ", ".join(e(n) for n in digest.failed)]
    if digest.silent:
        out += ["", "😶 Нет сообщений за день: " + ", ".join(e(n) for n in digest.silent)]
    if digest.sites:
        out += ["", "Подробный отчёт по объекту — кнопками ниже."]
    return "\n".join(out)
