"""Сквозные тесты на реальном PostgreSQL (нужен TEST_DATABASE_URL).

Telegram API подменён MockedSession, LLM и Whisper — фейками.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetFile,
    GetMe,
    SendChatAction,
    SendMessage,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import Chat, File, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.db.models import (
    Company,
    DailyReport,
    Entry,
    EntryKind,
    EntryStatus,
    ReportJob,
    Site,
    User,
)
from app.db.repositories import EntryRepo
from app.services.llm import LLMClient
from app.services.reports import ReportService
from app.services.storage import LocalFileStorage
from app.worker.processor import EntryProcessor
from app.worker.reports import ReportQueue
from app.worker.runner import EntryQueue
from tests.test_services import fake_openai

TG_USER_ID = 555


class MockedSession(BaseSession):
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.files = files or {}

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=123456, is_bot=True, first_name="Bot", username="prorab_test_bot")
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path=method.file_id)
        if isinstance(method, SendMessage):
            return Message(
                message_id=10_000 + len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, SetMessageReaction | SendChatAction | AnswerCallbackQuery):
            return True
        if isinstance(method, EditMessageText):
            return True
        raise NotImplementedError(type(method).__name__)

    async def stream_content(self, url: str, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        yield self.files[url.rsplit("/", 1)[-1]]

    async def close(self) -> None:
        pass

    def sent_texts(self) -> list[str]:
        return [r.text for r in self.requests if isinstance(r, SendMessage | EditMessageText)]


class FakeTranscriber:
    def __init__(self, text: str = "Залили 12 кубов бетона в перекрытие") -> None:
        self.text = text
        self.calls: list[Path] = []

    async def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        return self.text


class FailingTranscriber:
    async def transcribe(self, path: Path) -> str:
        raise ConnectionError("whisper недоступен")


_msg_ids = count(1)


def make_update(**message_fields: Any) -> Update:
    message_id = next(_msg_ids)
    message = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": TG_USER_ID, "type": "private"},
        "from": {"id": TG_USER_ID, "is_bot": False, "first_name": "Иван", "last_name": "Петров"},
        **message_fields,
    }
    return Update.model_validate({"update_id": message_id, "message": message})


def callback_update(data: str) -> Update:
    update_id = next(_msg_ids)
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "ci",
                "data": data,
                "from": {"id": TG_USER_ID, "is_bot": False, "first_name": "Иван"},
                "message": {
                    "message_id": 1,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": TG_USER_ID, "type": "private"},
                    "text": "Выберите объект:",
                },
            },
        }
    )


REPORT_JSON = json.dumps(
    {
        "summary": "Забетонировано перекрытие 3 этажа.",
        "work_done": [
            {
                "description": "Бетонирование перекрытия",
                "quantity": 12,
                "unit": "м³",
                "entry_ids": [1, 2, 424242],
            }
        ],
        "issues": [],
        "materials_needed": [{"name": "Арматура А500 Ø12", "quantity": 2, "unit": "т"}],
        "schedule_risks": [{"description": "Нет крана на завтра", "severity": "high"}],
    },
    ensure_ascii=False,
)


@pytest.fixture
def tg() -> MockedSession:
    return MockedSession(files={"voice-file": b"OggS-fake-voice", "photo-file": b"jpeg-bytes"})


@pytest.fixture
def bot(tg: MockedSession) -> Bot:
    return Bot("123456:TEST", session=tg)


async def test_full_flow(sessionmaker, settings, bot, tg, tmp_path):
    llm_client = fake_openai([REPORT_JSON])
    dp = build_dispatcher(settings, sessionmaker)

    # 1. Регистрация руководителя и компании
    await dp.feed_update(bot, make_update(text="/start"))
    await dp.feed_update(bot, make_update(text="ООО Стройка"))
    # 2. Сообщение до выбора объекта сохраняется «без объекта»
    await dp.feed_update(bot, make_update(text="Привезли арматуру"))
    # 3. Создание объекта привязывает ранее присланное
    await dp.feed_update(bot, make_update(text="/new_object"))
    await dp.feed_update(bot, make_update(text="ЖК Северный"))
    assert "Привязал к нему ранее присланные сообщения: 1" in tg.sent_texts()[-1]

    # 4. Голосовое и фото с объекта
    voice = {
        "file_id": "voice-file",
        "file_unique_id": "v1",
        "duration": 7,
        "mime_type": "audio/ogg",
        "file_size": 15,
    }
    await dp.feed_update(bot, make_update(voice=voice))
    photo = [
        {
            "file_id": "photo-file",
            "file_unique_id": "p1",
            "width": 1280,
            "height": 960,
            "file_size": 10,
        }
    ]
    await dp.feed_update(bot, make_update(photo=photo, caption="Каркас плиты"))

    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
        user = await s.scalar(select(User))
        entries = list(await s.scalars(select(Entry).order_by(Entry.id)))
    assert company.name == "ООО Стройка"
    assert user.role == "owner" and user.current_site_id is not None
    assert [e.kind for e in entries] == [EntryKind.TEXT, EntryKind.VOICE, EntryKind.PHOTO]
    assert all(e.site_id == user.current_site_id for e in entries)
    assert [e.status for e in entries] == [
        EntryStatus.DONE,
        EntryStatus.PENDING,
        EntryStatus.PENDING,
    ]
    assert entries[2].text == "Каркас плиты"
    assert any(isinstance(r, SetMessageReaction) for r in tg.requests)

    # 5. Воркер: скачивает в архив и расшифровывает
    transcriber = FakeTranscriber()
    storage = LocalFileStorage(tmp_path / "media")
    worker = EntryQueue(sessionmaker, EntryProcessor(bot, storage, transcriber))
    for entry_id in await worker.claim(10):
        await worker.handle(entry_id)

    async with sessionmaker() as s:
        voice_entry = await s.get(Entry, entries[1].id)
        photo_entry = await s.get(Entry, entries[2].id)
    assert voice_entry.status == photo_entry.status == EntryStatus.DONE
    assert voice_entry.transcript == transcriber.text
    assert storage.path(voice_entry.file_path).read_bytes() == b"OggS-fake-voice"
    assert voice_entry.file_path.endswith(f"_{voice_entry.id}.ogg")
    assert len(photo_entry.file_sha256) == 64
    assert any(t.startswith(f"🎙 <i>{transcriber.text}</i>") for t in tg.sent_texts())

    # 6. Отчёт: бот ставит задачу, воркер собирает и присылает
    await dp.feed_update(bot, make_update(text="/report"))
    assert "⏳ Формирую отчёт по «ЖК Северный»" in tg.sent_texts()[-1]
    await dp.feed_update(bot, make_update(text="/report"))
    assert "уже формируется" in tg.sent_texts()[-1]

    reports = ReportQueue(sessionmaker, bot, ReportService(LLMClient(llm_client, "m")))
    job_ids = await reports.claim(10)
    assert len(job_ids) == 1  # повторный /report не создал вторую задачу
    await reports.handle(job_ids[0])
    report_text = tg.sent_texts()[-1]
    assert "ЖК Северный" in report_text
    assert "Бетонирование перекрытия — <b>12 м³</b>" in report_text
    assert "🔴 Нет крана на завтра" in report_text
    user_prompt = llm_client.chat.completions.calls[0]["messages"][1]["content"]
    assert "Привезли арматуру" in user_prompt
    assert transcriber.text in user_prompt
    assert "подпись: «Каркас плиты»" in user_prompt

    async with sessionmaker() as s:
        saved = await s.scalar(select(DailyReport))
    assert saved.entries_count == 3
    assert saved.data["work_done"][0]["entry_ids"] == [1, 2]  # выдуманный id отброшен


async def test_duplicate_update_is_ignored(sessionmaker, settings, bot):
    dp = build_dispatcher(settings, sessionmaker)
    await dp.feed_update(bot, make_update(text="/start"))
    await dp.feed_update(bot, make_update(text="ООО Стройка"))
    update = make_update(text="Сообщение")
    await dp.feed_update(bot, update)
    await dp.feed_update(bot, update)
    async with sessionmaker() as s:
        assert len(list(await s.scalars(select(Entry)))) == 1


async def test_foreman_sees_only_own_sites(sessionmaker, settings, bot, tg):
    async with sessionmaker() as s:
        company = Company(name="ООО Стройка", invite_code="CODE123")
        s.add(company)
        await s.flush()
        s.add_all(
            [
                Site(company_id=company.id, name="Склад", invite_code="SKLAD"),
                Site(company_id=company.id, name="Офис", invite_code="OFIS"),
            ]
        )
        await s.commit()

    dp = build_dispatcher(settings, sessionmaker)

    # Приглашение в компанию без объекта: объектов прораб пока не видит
    await dp.feed_update(bot, make_update(text="/start inv_CODE123"))
    assert "Вы подключены к компании «ООО Стройка»" in tg.sent_texts()[-1]
    assert "Вас пока не добавили ни на один объект" in tg.sent_texts()[-1]

    # Приглашение на объект: доступ и текущий объект
    await dp.feed_update(bot, make_update(text="/start site_SKLAD"))
    assert "Текущий объект: <b>Склад</b>" in tg.sent_texts()[-1]

    tg.requests.clear()
    await dp.feed_update(bot, make_update(text="/object"))
    keyboard = tg.requests[-1].reply_markup.inline_keyboard
    assert [row[0].text for row in keyboard] == ["✅ Склад", "➕ Новый объект"]

    # Чужой объект нельзя выбрать, даже подделав callback
    async with sessionmaker() as s:
        office = await s.scalar(select(Site).where(Site.name == "Офис"))
        user = await s.scalar(select(User))
    await dp.feed_update(bot, callback_update(f"site:{office.id}"))
    assert any(
        isinstance(r, AnswerCallbackQuery) and r.text == "Объект не найден" for r in tg.requests
    )
    assert user.role == "foreman"


async def test_manager_creates_site_invite(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot, "Склад")
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))

    await dp.feed_update(bot, make_update(text="/invite"))
    await dp.feed_update(bot, callback_update(f"invite:{site.id}"))
    assert f"start=site_{site.invite_code}" in tg.sent_texts()[-1]


async def _seed_entry(sessionmaker, **kw) -> int:
    async with sessionmaker() as s:
        company = Company(name="C", invite_code=f"c{next(_msg_ids)}")
        user = User(tg_id=next(_msg_ids), company=company)
        s.add_all([company, user])
        await s.flush()
        entry = Entry(
            company_id=company.id,
            user_id=user.id,
            kind=EntryKind.VOICE,
            work_date=date(2026, 9, 30),
            sent_at=datetime.now(UTC),
            tg_chat_id=TG_USER_ID,
            tg_message_id=next(_msg_ids),
            tg_file_id="voice-file",
            **kw,
        )
        s.add(entry)
        await s.commit()
        return entry.id


async def test_claim_skips_locked_and_respects_backoff(sessionmaker):
    ready = [await _seed_entry(sessionmaker) for _ in range(3)]
    await _seed_entry(sessionmaker, next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
    stale = await _seed_entry(
        sessionmaker,
        status=EntryStatus.PROCESSING,
        locked_at=datetime.now(UTC) - timedelta(hours=1),
    )

    async with sessionmaker() as s1, sessionmaker() as s2:
        # Две транзакции одновременно: задачи не должны задвоиться
        first, second = await asyncio.gather(
            EntryRepo(s1).claim_batch(2, stale_after=600),
            EntryRepo(s2).claim_batch(10, stale_after=600),
        )
        await s1.commit()
        await s2.commit()
    assert not set(first) & set(second)
    assert set(first) | set(second) == {*ready, stale}


async def test_worker_retries_then_fails(sessionmaker, bot, tg, tmp_path):
    entry_id = await _seed_entry(sessionmaker)
    processor = EntryProcessor(bot, LocalFileStorage(tmp_path), FailingTranscriber())
    worker = EntryQueue(sessionmaker, processor, max_attempts=2)

    assert await worker.claim(10) == [entry_id]
    await worker.handle(entry_id)
    async with sessionmaker() as s:
        entry = await s.get(Entry, entry_id)
    assert entry.status == EntryStatus.PENDING
    assert entry.next_attempt_at > datetime.now(UTC)
    assert "whisper недоступен" in entry.error
    assert entry.file_path is not None  # файл сохранён в архив до падения расшифровки

    assert await worker.claim(10) == []  # ждём бэкофф
    async with sessionmaker() as s:
        (await s.get(Entry, entry_id)).next_attempt_at = None
        await s.commit()
    assert await worker.claim(10) == [entry_id]
    await worker.handle(entry_id)
    async with sessionmaker() as s:
        entry = await s.get(Entry, entry_id)
    assert entry.status == EntryStatus.FAILED
    assert "Не получилось расшифровать" in tg.sent_texts()[-1]


def edited_update(message_id: int, **message_fields: Any) -> Update:
    message = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "edit_date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": TG_USER_ID, "type": "private"},
        "from": {"id": TG_USER_ID, "is_bot": False, "first_name": "Иван"},
        **message_fields,
    }
    return Update.model_validate({"update_id": next(_msg_ids), "edited_message": message})


async def register_owner_with_site(dp, bot, site_name: str = "ЖК Северный") -> None:
    await dp.feed_update(bot, make_update(text="/start"))
    await dp.feed_update(bot, make_update(text="ООО Стройка"))
    await dp.feed_update(bot, make_update(text="/new_object"))
    await dp.feed_update(bot, make_update(text=site_name))


async def test_transcript_fix_by_reply_and_edited_message(
    sessionmaker, settings, bot, tg, tmp_path
):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)

    text_update = make_update(text="Залили 10 кубов")
    await dp.feed_update(bot, text_update)
    voice = {"file_id": "voice-file", "file_unique_id": "v1", "duration": 3}
    await dp.feed_update(bot, make_update(voice=voice))

    worker = EntryQueue(
        sessionmaker,
        EntryProcessor(bot, LocalFileStorage(tmp_path), FakeTranscriber("Залили 12 кубов")),
    )
    for entry_id in await worker.claim(10):
        await worker.handle(entry_id)

    async with sessionmaker() as s:
        voice_entry = await s.scalar(select(Entry).where(Entry.kind == EntryKind.VOICE))
    assert voice_entry.transcript_message_id is not None
    assert "ответьте на это сообщение" in tg.sent_texts()[-1]

    # Прораб отвечает на расшифровку исправленным текстом
    bot_message = {
        "message_id": voice_entry.transcript_message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": TG_USER_ID, "type": "private"},
        "from": {"id": 123456, "is_bot": True, "first_name": "Bot"},
        "text": "🎙 Залили 12 кубов",
    }
    await dp.feed_update(bot, make_update(text="Залили 21 куб", reply_to_message=bot_message))
    assert "Расшифровка исправлена" in tg.sent_texts()[-1]

    # Прораб редактирует текстовое сообщение
    await dp.feed_update(bot, edited_update(text_update.message.message_id, text="Залили 11 кубов"))

    async with sessionmaker() as s:
        entries = list(await s.scalars(select(Entry).order_by(Entry.id)))
    assert len(entries) == 2  # ответ-исправление не создаёт новую запись
    text_entry, voice_entry = entries
    assert text_entry.text == "Залили 11 кубов" and text_entry.edited_at is not None
    assert voice_entry.transcript == "Залили 21 куб"
    assert voice_entry.transcript_original == "Залили 12 кубов"


async def test_report_waits_for_unprocessed_entries(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    voice = {"file_id": "voice-file", "file_unique_id": "v1", "duration": 3}
    await dp.feed_update(bot, make_update(voice=voice))  # ещё не расшифровано
    await dp.feed_update(bot, make_update(text="/report"))

    llm_client = fake_openai([REPORT_JSON])
    reports = ReportQueue(sessionmaker, bot, ReportService(LLMClient(llm_client, "m")))
    [job_id] = await reports.claim(10)
    await reports.handle(job_id)

    async with sessionmaker() as s:
        job = await s.get(ReportJob, job_id)
    assert job.status == EntryStatus.PENDING and job.attempts == 0
    assert job.next_attempt_at is not None
    assert llm_client.chat.completions.calls == []  # LLM не вызывали


async def test_report_failure_is_reported_after_attempts(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, make_update(text="Залили 10 кубов"))
    await dp.feed_update(bot, make_update(text="/report"))

    reports = ReportQueue(
        sessionmaker,
        bot,
        ReportService(LLMClient(fake_openai(["мусор"] * 4), "m")),
        max_attempts=1,
    )
    [job_id] = await reports.claim(10)
    await reports.handle(job_id)

    async with sessionmaker() as s:
        job = await s.get(ReportJob, job_id)
    assert job.status == EntryStatus.FAILED
    assert "Не удалось сформировать отчёт" in tg.sent_texts()[-1]
