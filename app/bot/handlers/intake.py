"""Приём сообщений с объекта: текст, фото, голосовые, видео, файлы.

Хендлер только фиксирует сообщение в БД и сразу отвечает. Скачивание файла,
расшифровку и описание фото делает воркер (app/worker).
"""

import logging
import time
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter, StateFilter
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany
from app.bot.handlers.sites import no_sites_text
from app.bot.keyboards import Retranscribe, sites_keyboard
from app.config import Settings
from app.db.models import AUDIO_KINDS, Entry, EntryKind, EntryStatus, User
from app.db.repositories import EntryRepo, SiteRepo
from app.timeutils import work_date_for
from app.worker.processor import RETRANSCRIBING_TEXT

log = logging.getLogger(__name__)

router = Router(name="intake")
router.message.filter(HasCompany(), StateFilter(None))
router.edited_message.filter(HasCompany())
router.callback_query.filter(HasCompany())

# Лимит Bot API на скачивание файлов (без собственного Bot API сервера)
MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024

# Альбом приходит отдельными сообщениями — спрашиваем объект один раз на альбом
_prompted_albums: dict[str, float] = {}


def _first_in_album(media_group_id: str | None) -> bool:
    if media_group_id is None:
        return True
    now = time.monotonic()
    for key, ts in list(_prompted_albums.items()):
        if now - ts > 600:
            del _prompted_albums[key]
    if media_group_id in _prompted_albums:
        return False
    _prompted_albums[media_group_id] = now
    return True


def _new_entry(message: Message, user: User, settings: Settings, kind: EntryKind) -> Entry:
    # Для пересланных сообщений берём время оригинала: прораб мог переслать вчерашнее фото
    sent_at = message.forward_origin.date if message.forward_origin else message.date
    return Entry(
        company_id=user.company_id,
        site_id=user.current_site_id,
        user_id=user.id,
        kind=kind,
        status=EntryStatus.PENDING,
        work_date=work_date_for(sent_at, user.company.timezone, settings.work_day_start_hour),
        sent_at=sent_at,
        tg_chat_id=message.chat.id,
        tg_message_id=message.message_id,
        media_group_id=message.media_group_id,
        text=message.caption,
    )


async def _save_and_ack(message: Message, entry: Entry, user: User, session: AsyncSession) -> None:
    too_big = entry.file_size is not None and entry.file_size > MAX_DOWNLOAD_SIZE
    if too_big:
        entry.status = EntryStatus.DONE
        entry.error = "file too big for Bot API"

    if not await EntryRepo(session).add(entry):
        return  # дубль апдейта

    if too_big:
        await message.reply(
            "⚠️ Файл больше 20 МБ — сохранить его я не смогу. Подпись учту в отчёте. "
            "Видео лучше отправлять короче или в сжатом виде."
        )

    if entry.site_id is None:
        if not _first_in_album(entry.media_group_id):
            return
        sites = await SiteRepo(session).list_for_user(user)
        if sites:
            await message.reply(
                "Сохранил. К какому объекту это относится?",
                reply_markup=sites_keyboard(sites, None, can_create=user.is_manager),
            )
        else:
            await message.reply("Сохранил. " + no_sites_text(user))
        return

    await _react(message, "👀" if entry.kind in AUDIO_KINDS else "👍")


async def _react(message: Message, emoji: str) -> None:
    try:
        await message.react([ReactionTypeEmoji(emoji=emoji)])
    except TelegramAPIError:
        log.debug("Реакции недоступны в чате %s", message.chat.id)


class TranscriptReply(Filter):
    """Ответ на сообщение бота с расшифровкой — это исправление расшифровки."""

    async def __call__(self, message: Message, session: AsyncSession) -> bool | dict:
        reply = message.reply_to_message
        if reply is None or reply.from_user is None or not reply.from_user.is_bot:
            return False
        entry = await EntryRepo(session).get_by_transcript_message(
            message.chat.id, reply.message_id
        )
        return {"entry": entry} if entry else False


@router.message(F.text & ~F.text.startswith("/"), TranscriptReply())
async def on_transcript_fix(message: Message, entry: Entry) -> None:
    if entry.transcript_original is None:
        entry.transcript_original = entry.transcript
    entry.transcript = message.text.strip()
    entry.edited_at = datetime.now(UTC)
    await message.reply("✅ Расшифровка исправлена — в отчёт пойдёт ваш вариант.")


@router.callback_query(Retranscribe.filter())
async def on_retranscribe(
    call: CallbackQuery, callback_data: Retranscribe, user: User, session: AsyncSession
) -> None:
    entry = await session.get(Entry, callback_data.entry_id)
    if (
        entry is None
        or entry.company_id != user.company_id
        or (entry.user_id != user.id and not user.is_manager)
        or entry.kind not in AUDIO_KINDS
        or entry.file_path is None
    ):
        await call.answer("Голосовое не найдено", show_alert=True)
        return
    if entry.retranscribe or entry.status != EntryStatus.DONE:
        await call.answer("Уже распознаю — результат появится в этом сообщении")
        return
    # Воркер подхватит запись из очереди и разберёт её более точной моделью
    entry.retranscribe = True
    entry.status = EntryStatus.PENDING
    entry.attempts = 0
    entry.next_attempt_at = None
    entry.locked_at = None
    if call.message is not None:
        entry.transcript_message_id = call.message.message_id
    await call.answer("Распознаю заново")
    if isinstance(call.message, Message):
        try:
            await call.message.edit_text(RETRANSCRIBING_TEXT)
        except TelegramAPIError:
            log.debug("Не удалось обновить сообщение с расшифровкой entry=%s", entry.id)


@router.edited_message(F.text | F.caption)
async def on_edited(message: Message, session: AsyncSession) -> None:
    entry = await EntryRepo(session).get_by_message(message.chat.id, message.message_id)
    if entry is None:
        return
    new_text = message.text if entry.kind == EntryKind.TEXT else message.caption
    if new_text == entry.text:
        return
    entry.text = new_text
    entry.edited_at = datetime.now(UTC)
    await _react(message, "✍")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, user: User, session: AsyncSession, settings: Settings) -> None:
    entry = _new_entry(message, user, settings, EntryKind.TEXT)
    entry.text = message.text
    entry.status = EntryStatus.DONE  # обрабатывать нечего
    await _save_and_ack(message, entry, user, session)


@router.message(F.photo)
async def on_photo(message: Message, user: User, session: AsyncSession, settings: Settings) -> None:
    photo = message.photo[-1]  # самое большое разрешение
    entry = _new_entry(message, user, settings, EntryKind.PHOTO)
    entry.tg_file_id = photo.file_id
    entry.tg_file_unique_id = photo.file_unique_id
    entry.file_size = photo.file_size
    entry.mime_type = "image/jpeg"
    await _save_and_ack(message, entry, user, session)


@router.message(F.document)
async def on_document(
    message: Message, user: User, session: AsyncSession, settings: Settings
) -> None:
    doc = message.document
    is_image = (doc.mime_type or "").startswith("image/")
    # Фото «файлом» — оригинал с EXIF, лучший вариант для доказательной базы
    entry = _new_entry(message, user, settings, EntryKind.PHOTO if is_image else EntryKind.DOCUMENT)
    entry.tg_file_id = doc.file_id
    entry.tg_file_unique_id = doc.file_unique_id
    entry.file_size = doc.file_size
    entry.file_name = doc.file_name
    entry.mime_type = doc.mime_type
    await _save_and_ack(message, entry, user, session)


@router.message(F.voice)
async def on_voice(message: Message, user: User, session: AsyncSession, settings: Settings) -> None:
    voice = message.voice
    entry = _new_entry(message, user, settings, EntryKind.VOICE)
    entry.tg_file_id = voice.file_id
    entry.tg_file_unique_id = voice.file_unique_id
    entry.file_size = voice.file_size
    entry.mime_type = voice.mime_type or "audio/ogg"
    entry.duration = voice.duration
    await _save_and_ack(message, entry, user, session)


@router.message(F.audio)
async def on_audio(message: Message, user: User, session: AsyncSession, settings: Settings) -> None:
    audio = message.audio
    entry = _new_entry(message, user, settings, EntryKind.AUDIO)
    entry.tg_file_id = audio.file_id
    entry.tg_file_unique_id = audio.file_unique_id
    entry.file_size = audio.file_size
    entry.file_name = audio.file_name
    entry.mime_type = audio.mime_type
    entry.duration = audio.duration
    await _save_and_ack(message, entry, user, session)


@router.message(F.video_note)
async def on_video_note(
    message: Message, user: User, session: AsyncSession, settings: Settings
) -> None:
    note = message.video_note
    entry = _new_entry(message, user, settings, EntryKind.VIDEO_NOTE)
    entry.tg_file_id = note.file_id
    entry.tg_file_unique_id = note.file_unique_id
    entry.file_size = note.file_size
    entry.mime_type = "video/mp4"
    entry.duration = note.duration
    await _save_and_ack(message, entry, user, session)


@router.message(F.video)
async def on_video(message: Message, user: User, session: AsyncSession, settings: Settings) -> None:
    video = message.video
    entry = _new_entry(message, user, settings, EntryKind.VIDEO)
    entry.tg_file_id = video.file_id
    entry.tg_file_unique_id = video.file_unique_id
    entry.file_size = video.file_size
    entry.file_name = video.file_name
    entry.mime_type = video.mime_type or "video/mp4"
    entry.duration = video.duration
    await _save_and_ack(message, entry, user, session)
