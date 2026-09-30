from collections import Counter
from collections.abc import Iterable
from datetime import date
from html import escape

from app.db.models import EntryKind
from app.reports.schema import Severity, SiteDailyReport

TG_MESSAGE_LIMIT = 4096

_SEVERITY_ICON = {Severity.HIGH: "🔴", Severity.MEDIUM: "🟠", Severity.LOW: "🟡"}
_KIND_ICON = {
    EntryKind.TEXT: "✏️",
    EntryKind.VOICE: "🎙",
    EntryKind.AUDIO: "🎙",
    EntryKind.VIDEO_NOTE: "📹",
    EntryKind.PHOTO: "📷",
    EntryKind.VIDEO: "🎬",
    EntryKind.DOCUMENT: "📎",
}


def fmt_quantity(quantity: float | None, unit: str | None) -> str:
    if quantity is None:
        return ""
    num = f"{quantity:g}".replace(".", ",")
    return f"{num} {unit}".strip() if unit else num


def _section(title: str, lines: Iterable[str]) -> list[str]:
    lines = list(lines)
    return [f"\n<b>{title}</b>", *lines] if lines else []


def render_report(
    report: SiteDailyReport,
    site_name: str,
    work_date: date,
    kinds: Counter[str],
    unprocessed: int = 0,
) -> str:
    e = escape
    stats = " ".join(f"{_KIND_ICON.get(k, '•')}{n}" for k, n in kinds.most_common())
    out = [
        f"📋 <b>{e(site_name)}</b>",
        f"📅 {work_date:%d.%m.%Y} · сообщений: {sum(kinds.values())} {stats}",
    ]
    if unprocessed:
        out.append(f"⏳ Ещё обрабатывается: {unprocessed} — в отчёт не вошли")
    out.append(f"\n{e(report.summary)}")

    work = []
    for w in report.work_done:
        line = f"• {e(w.description)}"
        if q := fmt_quantity(w.quantity, w.unit):
            line += f" — <b>{e(q)}</b>"
        if w.location:
            line += f" ({e(w.location)})"
        work.append(line)
    out += _section("✅ Выполнено", work)

    resources = []
    if report.workforce:
        resources.append(f"👷 Людей на объекте: {report.workforce}")
    if report.equipment:
        resources.append(f"🚜 Техника: {e(', '.join(report.equipment))}")
    if resources:
        out += ["", *resources]

    out += _section(
        "⚠️ Проблемы",
        (f"{_SEVERITY_ICON[i.severity]} {e(i.description)}" for i in report.issues),
    )

    materials = []
    for m in report.materials_needed:
        line = f"• {e(m.name)}"
        if q := fmt_quantity(m.quantity, m.unit):
            line += f" — {e(q)}"
        if m.needed_by:
            line += f", к: {e(m.needed_by)}"
        if m.comment:
            line += f" <i>({e(m.comment)})</i>"
        materials.append(line)
    out += _section("📦 Нужны материалы", materials)

    risks = []
    for r in report.schedule_risks:
        line = f"{_SEVERITY_ICON[r.severity]} {e(r.description)}"
        if r.mitigation:
            line += f"\n    → <i>{e(r.mitigation)}</i>"
        risks.append(line)
    out += _section("⏰ Риски срыва сроков", risks)

    out += _section("➡️ План на завтра", (f"• {e(p)}" for p in report.plans_for_tomorrow))
    return "\n".join(out)


def split_message(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный отчёт по строкам, чтобы не разорвать HTML-теги."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # одиночная сверхдлинная строка
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
