from collections import Counter
from datetime import UTC, date, datetime

import pytest

from app.bot.handlers.reports import parse_days_ago
from app.db.models import Entry, EntryKind, Site, User
from app.reports.prompts import format_entry, group_albums, report_user_prompt
from app.reports.render import fmt_quantity, render_report, split_message
from app.reports.schema import Severity, SiteDailyReport
from app.services.llm import LLMError, extract_json


def test_schema_is_lenient_to_llm_output():
    report = SiteDailyReport.model_validate(
        {
            "summary": "Залили перекрытие",
            "work_done": [{"description": "Бетонирование", "quantity": "12,5 м3", "unit": "м³"}],
            "workforce": "8 человек",
            "issues": [{"description": "Нет крана", "severity": "Высокий"}],
            "schedule_risks": [{"description": "Дожди", "severity": "critical"}],
        }
    )
    assert report.work_done[0].quantity == 12.5
    assert report.workforce == 8
    assert report.issues[0].severity is Severity.HIGH
    assert report.schedule_risks[0].severity is Severity.HIGH
    assert report.materials_needed == []


def test_drop_unknown_entry_ids():
    report = SiteDailyReport.model_validate(
        {"summary": "-", "work_done": [{"description": "x", "entry_ids": [1, 2, 999]}]}
    )
    report.drop_unknown_entry_ids({1, 2})
    assert report.work_done[0].entry_ids == [1, 2]


def test_extract_json_from_markdown_fence():
    assert extract_json('Вот отчёт:\n```json\n{"a": 1}\n```') == '{"a": 1}'
    with pytest.raises(LLMError):
        extract_json("нет json")


def test_fmt_quantity():
    assert fmt_quantity(45.0, "м³") == "45 м³"
    assert fmt_quantity(12.5, "т") == "12,5 т"
    assert fmt_quantity(None, "т") == ""


def test_render_escapes_html_and_skips_empty_sections():
    report = SiteDailyReport.model_validate(
        {
            "summary": "Работы <по плану>",
            "work_done": [
                {"description": "Кладка", "quantity": 20, "unit": "м³", "location": "2 этаж"}
            ],
            "materials_needed": [
                {"name": "Газоблок D500", "quantity": 10, "unit": "м³", "needed_by": "пятница"}
            ],
        }
    )
    text = render_report(
        report, "ЖК <Север>", date(2026, 9, 30), Counter({"voice": 2, "photo": 3}), unprocessed=1
    )
    assert "ЖК &lt;Север&gt;" in text
    assert "Работы &lt;по плану&gt;" in text
    assert "Кладка — <b>20 м³</b> (2 этаж)" in text
    assert "Газоблок D500 — 10 м³, к: пятница" in text
    assert "Ещё обрабатывается: 1" in text
    assert "Проблемы" not in text  # пустой раздел не выводится


def test_split_message_respects_limit():
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text


def _entry(**kw) -> Entry:
    base = dict(
        id=7,
        kind=EntryKind.TEXT,
        sent_at=datetime(2026, 9, 30, 6, 15, tzinfo=UTC),
        user=User(full_name="Иван Петров"),
    )
    base.update(kw)
    return Entry(**base)


def test_format_entry_variants():
    assert (
        format_entry(_entry(text="Залили 12 кубов"), "Europe/Moscow")
        == "#7 [09:15] Иван Петров, текст: Залили 12 кубов"
    )
    photo = _entry(kind=EntryKind.PHOTO, text="Арматура", photo_description="Каркас плиты")
    assert format_entry(photo, "Europe/Moscow").endswith(
        "фото: подпись: «Арматура»; на фото: Каркас плиты"
    )
    assert "без подписи" in format_entry(_entry(kind=EntryKind.PHOTO), "Europe/Moscow")
    # голосовое без расшифровки в отчёт не идёт
    assert format_entry(_entry(kind=EntryKind.VOICE), "Europe/Moscow") is None


def test_report_user_prompt():
    site = Site(name="Склад", address="ул. Ленина, 1")
    prompt = report_user_prompt(
        site,
        date(2026, 9, 30),
        [_entry(kind=EntryKind.VOICE, transcript="Привезли бетон")],
        "Europe/Moscow",
    )
    assert "Объект: Склад" in prompt
    assert "Дата: 30.09.2026" in prompt
    assert "голосовое: Привезли бетон" in prompt


@pytest.mark.parametrize(
    ("args", "expected"),
    [(None, 0), ("вчера", 1), ("Позавчера", 2), ("3", 3), ("500", 60), ("что-то", 0)],
)
def test_parse_days_ago(args, expected):
    assert parse_days_ago(args) == expected


def test_album_is_one_line_with_shared_caption():
    first = _entry(id=1, kind=EntryKind.PHOTO, media_group_id="g", photo_description="Опалубка")
    second = _entry(id=2, kind=EntryKind.PHOTO, media_group_id="g", text="Перекрытие 3 этажа")
    third = _entry(id=3, text="Отдельное сообщение")
    groups = group_albums([first, third, second])
    assert [[e.id for e in g] for g in groups] == [[1, 2], [3]]

    prompt = report_user_prompt(
        Site(name="Склад"), date(2026, 9, 30), [first, third, second], "UTC"
    )
    assert "#1, #2 [06:15] Иван Петров, альбом (2 шт.): подпись: «Перекрытие 3 этажа»" in prompt
    assert "на фото: #1 — Опалубка" in prompt


def test_system_prompt_schema_is_compact():
    import json

    from app.reports.prompts import compact_schema, report_system_prompt

    full = json.dumps(SiteDailyReport.model_json_schema(), ensure_ascii=False)
    prompt = report_system_prompt()
    assert '"title"' not in prompt and "anyOf" not in prompt
    assert len(prompt) < 3 * len(full) // 4 + 1500
    # Сжатие не теряет полей
    compact = compact_schema(SiteDailyReport.model_json_schema())
    assert set(compact["properties"]) == set(SiteDailyReport.model_fields)
    assert compact["$defs"]["WorkItem"]["properties"]["quantity"]["type"] == ["number", "null"]
