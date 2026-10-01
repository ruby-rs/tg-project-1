import asyncio
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from app.reports.prompts import PHOTO_SYSTEM_PROMPT
from app.services.llm import LLMClient

log = logging.getLogger(__name__)

EXIF_IFD = 0x8769
TAG_DATETIME = 306
TAG_DATETIME_ORIGINAL = 36867


def extract_taken_at(data: bytes, tz_name: str) -> datetime | None:
    """Время съёмки из EXIF.

    Telegram вырезает EXIF у сжатых фото, поэтому оно есть только у снимков,
    отправленных «файлом». Время камеры считаем временем часового пояса компании.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
            raw = exif.get_ifd(EXIF_IFD).get(TAG_DATETIME_ORIGINAL) or exif.get(TAG_DATETIME)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        naive = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def shrink_for_llm(data: bytes, max_side: int = 1280) -> bytes:
    """Уменьшаем фото перед отправкой в модель: дешевле и быстрее, детали сохраняются."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()


class PhotoDescriber:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def describe(self, data: bytes, caption: str | None) -> str:
        image = await asyncio.to_thread(shrink_for_llm, data)
        return await self._llm.describe_image(image, "image/jpeg", PHOTO_SYSTEM_PROMPT, caption)
