import json
from collections.abc import Sequence
from datetime import date

from app.db.models import Entry, EntryKind, Site
from app.reports.schema import SiteDailyReport
from app.timeutils import to_local

REPORT_SYSTEM_PROMPT = """\
Ты — опытный инженер ПТО и руководитель проекта в строительной компании.
Прораб в течение дня присылал с объекта сообщения: текст, расшифровки голосовых,
подписи и описания фотографий. Сообщения хаотичные, с разговорной речью и ошибками
распознавания. Составь из них структурированный дневной отчёт для руководителя.

Правила:
- Используй только факты из сообщений. Ничего не выдумывай: нет объёма — quantity = null.
- Сохраняй единицы измерения (м³, м², п.м., т, шт). «Кубы» — м³, «квадраты» — м².
- Объединяй повторы: если про одну работу говорили несколько раз, это один пункт.
- work_done — только фактически выполненное сегодня, а не планы.
- issues — то, что уже случилось (брак, простой, поломка, нет доступа, замечания заказчика).
- materials_needed — что заказать/довезти, сколько и к какому сроку.
- schedule_risks — что может сорвать сроки: нехватка материалов или людей, простои,
  погода, ожидание решений заказчика, техники, отставание от графика. Оцени severity:
  high — срыв вероятен в ближайшие дни, medium — возможен, low — стоит держать в уме.
- В entry_ids указывай номера сообщений (#N), на которых основан пункт.
- Если по разделу ничего нет — пустой список.
- Пиши по-русски, кратко и по делу, без воды.

Ответ — строго один JSON-объект по схеме ниже, без markdown и пояснений.
JSON Schema:
"""

PHOTO_SYSTEM_PROMPT = """\
Ты — инженер строительного контроля. Опиши фото с объекта строительства для дневного
отчёта: какой вид работ и этап, что видно по объёму и качеству, материалы, техника,
явные нарушения технологии или техники безопасности. 2–4 предложения, только то,
что действительно видно. Если фото не относится к стройке — так и напиши."""


def report_system_prompt() -> str:
    schema = json.dumps(SiteDailyReport.model_json_schema(), ensure_ascii=False)
    return REPORT_SYSTEM_PROMPT + schema


_KIND_LABELS = {
    EntryKind.TEXT: "текст",
    EntryKind.VOICE: "голосовое",
    EntryKind.AUDIO: "аудио",
    EntryKind.VIDEO_NOTE: "видеокружок",
    EntryKind.PHOTO: "фото",
    EntryKind.VIDEO: "видео",
    EntryKind.DOCUMENT: "файл",
}


def format_entry(entry: Entry, tz_name: str) -> str | None:
    """Строка журнала для LLM. None — в сообщении нет содержательного текста."""
    parts: list[str] = []
    if entry.transcript:
        parts.append(entry.transcript.strip())
    if entry.text:
        label = "подпись" if entry.kind != EntryKind.TEXT else None
        parts.append(f"{label}: «{entry.text.strip()}»" if label else entry.text.strip())
    if entry.photo_description:
        parts.append(f"на фото: {entry.photo_description.strip()}")
    if not parts:
        if entry.kind in (EntryKind.PHOTO, EntryKind.VIDEO):
            parts.append("без подписи")
        else:
            return None

    time = to_local(entry.sent_at, tz_name).strftime("%H:%M")
    author = entry.user.full_name if entry.user else "прораб"
    return f"#{entry.id} [{time}] {author}, {_KIND_LABELS.get(entry.kind, entry.kind)}: " + (
        "; ".join(parts)
    )


def report_user_prompt(site: Site, work_date: date, entries: Sequence[Entry], tz_name: str) -> str:
    lines = [line for e in entries if (line := format_entry(e, tz_name))]
    header = [f"Объект: {site.name}"]
    if site.address:
        header.append(f"Адрес: {site.address}")
    header.append(f"Дата: {work_date:%d.%m.%Y}")
    return "\n".join(header) + "\n\nСообщения за день:\n" + "\n".join(lines)
