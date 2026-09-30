import io
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from app.db.models import Entry, EntryKind
from app.reports.schema import SiteDailyReport
from app.services.llm import LLMClient, LLMError
from app.services.photos import extract_taken_at, shrink_for_llm
from app.services.storage import LocalFileStorage
from app.timeutils import work_date_for
from app.worker.processor import file_extension
from app.worker.runner import retry_delay


def test_work_date_uses_company_timezone():
    # 22:30 UTC — это уже следующий день по Москве
    dt = datetime(2026, 9, 29, 22, 30, tzinfo=UTC)
    assert work_date_for(dt, "Europe/Moscow") == date(2026, 9, 30)
    assert work_date_for(dt, "UTC") == date(2026, 9, 29)


def test_work_date_night_messages_belong_to_previous_day():
    dt = datetime(2026, 9, 29, 22, 30, tzinfo=UTC)  # 01:30 по Москве
    assert work_date_for(dt, "Europe/Moscow", day_start_hour=4) == date(2026, 9, 29)


async def test_storage_is_write_once(tmp_path):
    storage = LocalFileStorage(tmp_path)
    key = storage.build_key(3, datetime(2026, 9, 30, 9, 5, 7), 42, ".jpg")
    assert key == "3/2026/09/30/090507_42.jpg"

    stored = await storage.save(key, b"photo")
    assert storage.path(key).read_bytes() == b"photo"
    assert stored.size == 5 and len(stored.sha256) == 64

    # Повтор с тем же содержимым — ок (ретрай воркера), с другим — ошибка
    assert (await storage.save(key, b"photo")).sha256 == stored.sha256
    with pytest.raises(FileExistsError):
        await storage.save(key, b"other")


def test_storage_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        LocalFileStorage(tmp_path).path("../../etc/passwd")


def _jpeg(exif_dt: str | None = None) -> bytes:
    img = Image.new("RGB", (2000, 1000), "gray")
    out = io.BytesIO()
    exif = Image.Exif()
    if exif_dt:
        exif.get_ifd(0x8769)[36867] = exif_dt
    img.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_extract_taken_at():
    taken = extract_taken_at(_jpeg("2026:09:30 08:12:00"), "Europe/Moscow")
    assert taken == datetime(2026, 9, 30, 5, 12, tzinfo=UTC)
    assert extract_taken_at(_jpeg(), "Europe/Moscow") is None
    assert extract_taken_at(b"not an image", "Europe/Moscow") is None


def test_shrink_for_llm():
    with Image.open(io.BytesIO(shrink_for_llm(_jpeg()))) as img:
        assert max(img.size) == 1280


@pytest.mark.parametrize(
    ("kind", "file_name", "mime", "ext"),
    [
        (EntryKind.VOICE, None, "audio/ogg", ".ogg"),
        (EntryKind.PHOTO, None, "image/jpeg", ".jpg"),
        (EntryKind.PHOTO, "IMG_0001.HEIC", "image/heic", ".heic"),
        (EntryKind.DOCUMENT, "смета.xlsx", None, ".xlsx"),
        (EntryKind.DOCUMENT, "bad.../../x", None, ".bin"),
    ],
)
def test_file_extension(kind, file_name, mime, ext):
    assert file_extension(Entry(kind=kind, file_name=file_name, mime_type=mime)) == ext


def test_retry_delay_grows_and_is_capped():
    assert retry_delay(1).total_seconds() == 30
    assert retry_delay(2).total_seconds() == 60
    assert retry_delay(10).total_seconds() == 900


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


async def test_llm_repairs_invalid_json():
    client = fake_openai(['{"work_done": []}', '{"summary": "ok"}'])
    llm = LLMClient(client, model="m")
    report = await llm.complete_json("sys", "user", SiteDailyReport)
    assert report.summary == "ok"
    calls = client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "Ответ не соответствует схеме" in calls[1]["messages"][-1]["content"]


async def test_llm_gives_up_after_repair_attempts():
    llm = LLMClient(fake_openai(["мусор", "снова мусор"]), model="m", json_mode=False)
    with pytest.raises(LLMError):
        await llm.complete_json("sys", "user", SiteDailyReport)
