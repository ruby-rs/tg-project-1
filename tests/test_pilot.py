"""Оценка отчётов и статистика для пилота."""

from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.handlers.feedback import RetryFailed
from app.db.models import Entry, EntryKind, EntryStatus, ReportFeedback
from app.services.llm import LLMClient
from app.services.reports import ReportService
from app.services.storage import LocalFileStorage
from app.worker.processor import EntryProcessor
from app.worker.reports import ReportQueue
from app.worker.runner import EntryQueue
from tests.helpers import (
    REPORT_JSON,
    FakeTranscriber,
    callback_update,
    fake_openai,
    make_update,
    register_owner_with_site,
)


async def _report_with_buttons(dp, bot, tg, sessionmaker):
    await dp.feed_update(bot, make_update(text="/report"))
    reports = ReportQueue(
        sessionmaker, bot, ReportService(LLMClient(fake_openai([REPORT_JSON]), "m"))
    )
    [job_id] = await reports.claim(10)
    await reports.handle(job_id)
    return tg.requests[-1]


async def test_report_feedback(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, make_update(text="Залили 12 кубов"))

    report_msg = await _report_with_buttons(dp, bot, tg, sessionmaker)
    like, dislike = report_msg.reply_markup.inline_keyboard[0]
    assert (like.text, dislike.text) == ("👍 Полезно", "👎 Есть ошибки")

    await dp.feed_update(bot, callback_update(dislike.callback_data))
    assert "Что не так в отчёте?" in tg.sent_texts()[-1]
    await dp.feed_update(bot, make_update(text="Объём не 12, а 21 куб"))
    assert "замечание записал" in tg.sent_texts()[-1]

    async with sessionmaker() as s:
        fb = await s.scalar(select(ReportFeedback))
    assert fb.rating == -1 and fb.comment == "Объём не 12, а 21 куб"

    # Передумал — оценка меняется, а не дублируется
    await dp.feed_update(bot, callback_update(like.callback_data))
    async with sessionmaker() as s:
        rows = list(await s.scalars(select(ReportFeedback)))
    assert len(rows) == 1 and rows[0].rating == 1


async def test_stats_and_retry_failed(sessionmaker, settings, bot, tg, tmp_path):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, make_update(text="Привезли арматуру"))
    voice = {"file_id": "voice-file", "file_unique_id": "v1", "duration": 3}
    await dp.feed_update(bot, make_update(voice=voice))
    await dp.feed_update(bot, make_update(voice={**voice, "file_unique_id": "v2"}))

    entries = EntryQueue(
        sessionmaker, EntryProcessor(bot, LocalFileStorage(tmp_path), FakeTranscriber("Залили"))
    )
    for entry_id in await entries.claim(10):
        await entries.handle(entry_id)

    async with sessionmaker() as s:
        voices = list(
            await s.scalars(select(Entry).where(Entry.kind == EntryKind.VOICE).order_by(Entry.id))
        )
        voices[0].transcript_original, voices[0].transcript = "Залили", "Залили 12 кубов"
        voices[1].status = EntryStatus.FAILED  # будто Whisper был недоступен
        await s.commit()

    report_msg = await _report_with_buttons(dp, bot, tg, sessionmaker)
    await dp.feed_update(
        bot, callback_update(report_msg.reply_markup.inline_keyboard[0][1].callback_data)
    )
    await dp.feed_update(bot, make_update(text="Пропущена разгрузка"))

    tg.requests.clear()
    await dp.feed_update(bot, make_update(text="/stats"))
    stats_msg = tg.requests[-1]
    text = stats_msg.text
    assert "Сообщений: 3" in text
    assert "Расшифровано голосовых: 2 из 2" in text
    assert "Исправлено прорабами: 1 (50%)" in text
    assert "Отчётов собрано: 1, с ошибкой: 0" in text
    assert "👍 0 · 👎 1" in text
    assert "«Пропущена разгрузка»" in text
    assert "❌ Не обработано: 1" in text

    retry = stats_msg.reply_markup.inline_keyboard[0][0]
    assert retry.callback_data == RetryFailed().pack()
    await dp.feed_update(bot, callback_update(retry.callback_data))
    async with sessionmaker() as s:
        entry = await s.get(Entry, voices[1].id)
    assert entry.status == EntryStatus.PENDING and entry.attempts == 0
