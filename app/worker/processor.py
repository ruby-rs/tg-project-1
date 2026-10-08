import io
import logging
import mimetypes
from datetime import UTC, datetime
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import transcript_keyboard
from app.db.models import AUDIO_KINDS, Entry, EntryKind
from app.db.repositories import EntryRepo
from app.services.photos import PhotoDescriber, extract_taken_at
from app.services.storage import LocalFileStorage
from app.services.transcription import Transcriber
from app.timeutils import to_local

log = logging.getLogger(__name__)

TRANSCRIPT_HINT = "Если есть ошибки — ответьте на это сообщение исправленным текстом."
RETRY_HINT = "Или нажмите «Распознать заново» — разберу точнее."
NO_SPEECH_TEXT = (
    "🎙 Не удалось разобрать речь. Нажмите «Распознать заново» или ответьте "
    "на это сообщение текстом."
)
RETRANSCRIBING_TEXT = "🎙 Распознаю заново, более точно. Это может занять пару минут…"

_DEFAULT_EXT = {
    EntryKind.VOICE: ".ogg",
    EntryKind.VIDEO_NOTE: ".mp4",
    EntryKind.PHOTO: ".jpg",
    EntryKind.VIDEO: ".mp4",
    EntryKind.AUDIO: ".mp3",
    EntryKind.DOCUMENT: ".bin",
}


def file_extension(entry: Entry) -> str:
    if entry.file_name and "." in entry.file_name:
        ext = "." + entry.file_name.rsplit(".", 1)[1].lower()
        if ext[1:].isalnum() and len(ext) <= 10:
            return ext
    if entry.kind not in (EntryKind.VOICE, EntryKind.VIDEO_NOTE) and entry.mime_type:
        if guessed := mimetypes.guess_extension(entry.mime_type):
            return guessed
    return _DEFAULT_EXT.get(entry.kind, ".bin")


def transcript_text(entry: Entry, note: str | None = None, *, with_button: bool = True) -> str:
    """Сообщение с расшифровкой голосового (note — строка о повторном разборе)."""
    if not entry.transcript:
        return f"{NO_SPEECH_TEXT}\n\n{note}" if note else NO_SPEECH_TEXT
    lines = [f"🎙 <i>{escape(entry.transcript[:3800])}</i>", ""]
    if note:
        lines.append(note)
    lines.append(TRANSCRIPT_HINT)
    if with_button:
        lines.append(RETRY_HINT)
    return "\n".join(lines)


class EntryProcessor:
    """Обработка одного сообщения: скачать файл в архив, расшифровать, описать фото."""

    def __init__(
        self,
        bot: Bot,
        storage: LocalFileStorage,
        transcriber: Transcriber,
        describer: PhotoDescriber | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.transcriber = transcriber
        self.describer = describer

    async def process(self, session: AsyncSession, entry: Entry) -> None:
        tz = entry.company.timezone
        data: bytes | None = None

        if entry.tg_file_id and not entry.file_path:
            buf = io.BytesIO()
            await self.bot.download(entry.tg_file_id, destination=buf)
            data = buf.getvalue()
            key = self.storage.build_key(
                entry.company_id, to_local(entry.sent_at, tz), entry.id, file_extension(entry)
            )
            stored = await self.storage.save(key, data)
            entry.file_path, entry.file_size, entry.file_sha256 = (
                stored.key,
                stored.size,
                stored.sha256,
            )
            if entry.kind == EntryKind.PHOTO:
                entry.taken_at = extract_taken_at(data, tz)

        # Контрольная точка: файл в архиве, даже если дальше упадёт расшифровка.
        # Заодно закрываем транзакцию — не держим её открытой, пока идёт Whisper
        await session.commit()

        if entry.kind in AUDIO_KINDS and entry.transcript is None and entry.file_path:
            entry.transcript = await self.transcriber.transcribe(self.storage.path(entry.file_path))

        if (
            entry.kind == EntryKind.PHOTO
            and self.describer is not None
            and entry.photo_description is None
            and entry.file_path
        ):
            if data is None:
                data = self.storage.path(entry.file_path).read_bytes()
            hint = entry.text
            if hint is None and entry.media_group_id:
                hint = await EntryRepo(session).album_caption(
                    entry.tg_chat_id, entry.media_group_id
                )
            try:
                entry.photo_description = await self.describer.describe(data, hint)
            except Exception:
                # Описание фото — не критично: в отчёт уйдёт хотя бы подпись
                log.exception("Не удалось описать фото entry=%s", entry.id)
                entry.photo_description = ""

    async def retranscribe(self, entry: Entry) -> bool:
        """Повторный разбор голосового по кнопке. True — текст изменился.

        Пустой результат прежнюю расшифровку не затирает.
        """
        assert entry.file_path is not None
        text = await self.transcriber.transcribe(self.storage.path(entry.file_path), accurate=True)
        if not text or text == entry.transcript:
            return False
        entry.transcript = text
        # Ручная правка относилась к прежнему тексту; новая расшифровка — новая точка отсчёта
        entry.transcript_original = None
        # Отчёт, собранный до повтора, устарел
        entry.edited_at = datetime.now(UTC)
        return True

    async def notify_done(self, entry: Entry) -> int | None:
        """Присылает расшифровку. Возвращает id сообщения бота, чтобы ответом на него
        прораб мог исправить расшифровку, а кнопкой под ним — распознать заново."""
        if entry.kind not in AUDIO_KINDS:
            return None
        sent = await self._reply(entry, transcript_text(entry), transcript_keyboard(entry.id))
        return sent.message_id if sent else None

    async def notify_retranscribed(
        self, entry: Entry, changed: bool | None, *, report_rebuild: bool = False
    ) -> int | None:
        """Обновляет сообщение с расшифровкой после повтора (changed=None — повтор упал).

        Возвращает id сообщения с расшифровкой, если пришлось отправить новое."""
        if changed:
            note = "🔁 Распознал заново."
            if report_rebuild:
                note += f" Отчёт за {entry.work_date:%d.%m} пересоберу и пришлю."
        elif changed is None:
            note = "⚠️ Повторно распознать не получилось — оставил прежний вариант."
        else:
            note = "🔁 Повторный разбор дал тот же результат."
        # Тот же результат при повторе не изменится — кнопку оставляем только после сбоя
        markup = transcript_keyboard(entry.id) if changed is None or not entry.transcript else None
        text = transcript_text(entry, note, with_button=markup is not None)
        if entry.transcript_message_id is not None:
            try:
                await self.bot.edit_message_text(
                    text,
                    chat_id=entry.tg_chat_id,
                    message_id=entry.transcript_message_id,
                    reply_markup=markup,
                )
                return None
            except Exception:
                log.warning("Не удалось обновить расшифровку entry=%s, шлю заново", entry.id)
        sent = await self._reply(entry, text, markup)
        return sent.message_id if sent else None

    async def notify_failed(self, entry: Entry) -> None:
        await self._reply(
            entry,
            "❌ Не получилось обработать это сообщение. Файл сохранён не был — "
            "пришлите его ещё раз или опишите текстом."
            if not entry.file_path
            else "❌ Не получилось расшифровать это сообщение. Файл сохранён в архиве, "
            "но в отчёт текст не попадёт — продублируйте главное текстом.",
        )

    async def _reply(
        self, entry: Entry, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message | None:
        try:
            return await self.bot.send_message(
                entry.tg_chat_id,
                text,
                reply_parameters=ReplyParameters(
                    message_id=entry.tg_message_id, allow_sending_without_reply=True
                ),
                reply_markup=markup,
            )
        except Exception:
            log.exception("Не удалось отправить уведомление по entry=%s", entry.id)
            return None
