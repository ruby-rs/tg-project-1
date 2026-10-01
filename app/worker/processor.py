import io
import logging
import mimetypes
from html import escape

from aiogram import Bot
from aiogram.types import Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AUDIO_KINDS, Entry, EntryKind
from app.db.repositories import EntryRepo
from app.services.photos import PhotoDescriber, extract_taken_at
from app.services.storage import LocalFileStorage
from app.services.transcription import Transcriber
from app.timeutils import to_local

log = logging.getLogger(__name__)

TRANSCRIPT_HINT = "Если есть ошибки — ответьте на это сообщение исправленным текстом."

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
            # Контрольная точка: файл в архиве, даже если дальше упадёт расшифровка
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

    async def notify_done(self, entry: Entry) -> int | None:
        """Присылает расшифровку. Возвращает id сообщения бота, чтобы ответом на него
        прораб мог исправить расшифровку."""
        if entry.kind not in AUDIO_KINDS:
            return None
        if entry.transcript:
            text = f"🎙 <i>{escape(entry.transcript[:3900])}</i>\n\n{TRANSCRIPT_HINT}"
        else:
            text = "🎙 Не удалось разобрать речь — продублируйте текстом, пожалуйста."
        sent = await self._reply(entry, text)
        return sent.message_id if sent and entry.transcript else None

    async def notify_failed(self, entry: Entry) -> None:
        await self._reply(
            entry,
            "❌ Не получилось обработать это сообщение. Файл сохранён не был — "
            "пришлите его ещё раз или опишите текстом."
            if not entry.file_path
            else "❌ Не получилось расшифровать это сообщение. Файл сохранён в архиве, "
            "но в отчёт текст не попадёт — продублируйте главное текстом.",
        )

    async def _reply(self, entry: Entry, text: str) -> Message | None:
        try:
            return await self.bot.send_message(
                entry.tg_chat_id,
                text,
                reply_parameters=ReplyParameters(
                    message_id=entry.tg_message_id, allow_sending_without_reply=True
                ),
            )
        except Exception:
            log.exception("Не удалось отправить уведомление по entry=%s", entry.id)
            return None
