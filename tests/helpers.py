"""Общие хелперы тестов: подмена Telegram API, фейки Whisper и LLM, сборка апдейтов."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
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
        if isinstance(method, EditMessageText | EditMessageReplyMarkup):
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


msg_ids = count(1)


def make_update(user_id: int = TG_USER_ID, **message_fields: Any) -> Update:
    message_id = next(msg_ids)
    first_name, last_name = ("Иван", "Петров") if user_id == TG_USER_ID else ("Сергей", "Котов")
    message = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": first_name, "last_name": last_name},
        **message_fields,
    }
    return Update.model_validate({"update_id": message_id, "message": message})


def callback_update(data: str, user_id: int = TG_USER_ID) -> Update:
    update_id = next(msg_ids)
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "ci",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Иван"},
                "message": {
                    "message_id": 1,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": user_id, "type": "private"},
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


def edited_update(message_id: int, **message_fields: Any) -> Update:
    message = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "edit_date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": TG_USER_ID, "type": "private"},
        "from": {"id": TG_USER_ID, "is_bot": False, "first_name": "Иван"},
        **message_fields,
    }
    return Update.model_validate({"update_id": next(msg_ids), "edited_message": message})


async def register_owner_with_site(dp, bot, site_name: str = "ЖК Северный") -> None:
    await dp.feed_update(bot, make_update(text="/start"))
    await dp.feed_update(bot, callback_update("consent:accept"))
    await dp.feed_update(bot, make_update(text="ООО Стройка"))
    await dp.feed_update(bot, make_update(text="/new_object"))
    await dp.feed_update(bot, make_update(text=site_name))


class FakeCompletions:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def fake_openai(answers: list[str]) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(answers)))
