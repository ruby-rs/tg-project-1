"""Сборник медиа к отчёту: фото, видео и файлы прорабов за день альбомами Telegram.

Файлы пересылаются по file_id — без скачивания и повторной загрузки, поэтому это
быстро и работает и в боте, и в воркере. Голосовые сюда не входят: их расшифровка
уже в отчёте, а исходники — в архиве объекта.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from app.db.models import Entry, EntryKind
from app.timeutils import to_local

log = logging.getLogger(__name__)

ALBUM_SIZE = 10  # лимит Telegram на альбом
CAPTION_LIMIT = 1024
# Пауза между альбомами: Telegram ограничивает частоту отправки в один чат
PAUSE = 1.0


@dataclass(slots=True)
class MediaCollection:
    visual: list[Entry] = field(default_factory=list)  # сжатые фото и видео
    files: list[Entry] = field(default_factory=list)  # фото «файлом» и документы
    notes: list[Entry] = field(default_factory=list)  # видеосообщения («кружки»)

    @property
    def total(self) -> int:
        return len(self.visual) + len(self.files) + len(self.notes)


def is_original_photo(entry: Entry) -> bool:
    """Фото, присланное «файлом»: у документа есть имя файла, у сжатого фото — нет."""
    return entry.kind == EntryKind.PHOTO and entry.file_name is not None


def collect_media(entries: Sequence[Entry]) -> MediaCollection:
    media = MediaCollection()
    for entry in entries:
        if not entry.tg_file_id:
            continue
        if entry.kind == EntryKind.VIDEO or (
            entry.kind == EntryKind.PHOTO and not is_original_photo(entry)
        ):
            media.visual.append(entry)
        elif entry.kind in (EntryKind.PHOTO, EntryKind.DOCUMENT):
            media.files.append(entry)
        elif entry.kind == EntryKind.VIDEO_NOTE:
            media.notes.append(entry)
    return media


def entry_caption(entry: Entry, tz_name: str) -> str:
    """«14:32 · Иван Петров — подпись прораба»."""
    caption = f"{to_local(entry.sent_at, tz_name):%H:%M}"
    if entry.user is not None and entry.user.full_name:
        caption += f" · {escape(entry.user.full_name)}"
    if entry.text:
        caption += f" — {escape(entry.text.strip())}"
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"
    return caption


def collection_title(media: MediaCollection, site_name: str, work_date: date) -> str:
    photos = sum(1 for e in media.visual if e.kind == EntryKind.PHOTO)
    videos = len(media.visual) - photos
    parts = [
        f"фото {photos}" if photos else "",
        f"видео {videos}" if videos else "",
        f"файлов {len(media.files)}" if media.files else "",
        f"кружков {len(media.notes)}" if media.notes else "",
    ]
    return f"📸 <b>Медиа за {work_date:%d.%m.%Y}</b> по «{escape(site_name)}»: " + ", ".join(
        p for p in parts if p
    )


def _chunks(items: list[Entry]) -> list[list[Entry]]:
    return [items[i : i + ALBUM_SIZE] for i in range(0, len(items), ALBUM_SIZE)]


def _visual_input(entry: Entry, caption: str) -> InputMediaPhoto | InputMediaVideo:
    if entry.kind == EntryKind.VIDEO:
        return InputMediaVideo(media=entry.tg_file_id, caption=caption)
    return InputMediaPhoto(media=entry.tg_file_id, caption=caption)


async def _call(make: Callable[[], Awaitable[Any]]) -> bool:
    """Один запрос к Telegram с повтором после flood-limit. False — не отправилось."""
    for _ in range(2):
        try:
            await make()
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramAPIError:
            log.warning("Не удалось отправить медиа", exc_info=True)
            return False
    return False


async def _send_single(bot: Bot, chat_id: int, entry: Entry, caption: str) -> bool:
    if entry.kind == EntryKind.VIDEO_NOTE:
        return await _call(lambda: bot.send_video_note(chat_id, entry.tg_file_id))
    if entry.kind == EntryKind.VIDEO:
        return await _call(lambda: bot.send_video(chat_id, entry.tg_file_id, caption=caption))
    if entry.kind == EntryKind.PHOTO and not is_original_photo(entry):
        return await _call(lambda: bot.send_photo(chat_id, entry.tg_file_id, caption=caption))
    return await _call(lambda: bot.send_document(chat_id, entry.tg_file_id, caption=caption))


async def _send_album(bot: Bot, chat_id: int, album: list[Entry], tz_name: str, *, files: bool):
    """Альбом до 10 штук. Если Telegram отверг альбом целиком — шлём по одному."""
    if len(album) > 1:
        if files:
            media = [
                InputMediaDocument(media=e.tg_file_id, caption=entry_caption(e, tz_name))
                for e in album
            ]
        else:
            media = [_visual_input(e, entry_caption(e, tz_name)) for e in album]
        if await _call(lambda: bot.send_media_group(chat_id, media)):
            return 0
    failed = 0
    for entry in album:
        if not await _send_single(bot, chat_id, entry, entry_caption(entry, tz_name)):
            failed += 1
    return failed


async def send_media_collection(
    bot: Bot,
    chat_id: int,
    entries: Sequence[Entry],
    site_name: str,
    work_date: date,
    tz_name: str,
) -> int:
    """Присылает сборник медиа за день. Возвращает, сколько файлов в нём было."""
    media = collect_media(entries)
    if not media.total:
        return 0
    if not await _call(
        lambda: bot.send_message(chat_id, collection_title(media, site_name, work_date))
    ):
        return 0

    failed = 0
    batches = [(a, False) for a in _chunks(media.visual)] + [
        (a, True) for a in _chunks(media.files)
    ]
    for i, (album, files) in enumerate(batches):
        if i:
            await asyncio.sleep(PAUSE)
        failed += await _send_album(bot, chat_id, album, tz_name, files=files)
    for note in media.notes:
        await asyncio.sleep(PAUSE)
        if not await _send_single(bot, chat_id, note, ""):
            failed += 1

    if failed:
        await _call(
            lambda: bot.send_message(
                chat_id,
                f"⚠️ Не удалось прислать файлов: {failed}. Они есть в архиве объекта — "
                "кнопка «🗂 Архив».",
            )
        )
    return media.total
