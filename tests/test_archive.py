"""Выгрузка архива объекта."""

import csv
import hashlib
import io
import zipfile
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.handlers.archive import ArchivePeriod
from app.db.models import Entry, EntryKind, ExportJob, Site, User
from app.services.archive import ArchiveTooLarge, build_archive
from app.services.storage import LocalFileStorage
from app.worker.exports import ExportQueue
from app.worker.processor import EntryProcessor
from app.worker.runner import EntryQueue
from tests.helpers import (
    FakeTranscriber,
    callback_update,
    make_update,
    register_owner_with_site,
)


def _entry(id_: int, kind: str, storage: LocalFileStorage | None = None, data: bytes = b"", **kw):
    entry = Entry(
        id=id_,
        kind=kind,
        work_date=date(2026, 10, 1),
        sent_at=datetime(2026, 10, 1, 6, 15, id_, tzinfo=UTC),
        user=User(full_name="Иван Петров"),
        **kw,
    )
    if storage is not None:
        key = f"1/2026/10/01/{id_}.jpg"
        path = storage.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entry.file_path = key
        entry.file_sha256 = hashlib.sha256(data).hexdigest()
    return entry


def _registry(zf: zipfile.ZipFile) -> list[list[str]]:
    text = zf.read("registry.csv").decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=";"))


def test_build_archive_parts_registry_and_checks(tmp_path):
    storage = LocalFileStorage(tmp_path / "media")
    photo1 = _entry(1, EntryKind.PHOTO, storage, b"a" * 600, text="Армирование")
    photo2 = _entry(2, EntryKind.PHOTO, storage, b"b" * 600)
    text = _entry(3, EntryKind.TEXT, text="Залили 12 кубов")
    # Файл подменили после получения — сверка должна это показать
    storage.path(photo2.file_path).write_bytes(b"x" * 600)

    out = tmp_path / "out"
    out.mkdir()
    result = build_archive(
        [photo1, photo2, text],
        storage,
        "Europe/Moscow",
        out,
        "arch",
        "Склад",
        "01.10.2026",
        part_limit=1000,
    )
    assert result.files == 2 and len(result.parts) == 2  # 600 + 600 > 1000 — две части
    assert [p.name for p in result.parts] == ["arch_part1.zip", "arch_part2.zip"]

    with zipfile.ZipFile(result.parts[0]) as zf:
        names = zf.namelist()
        assert "README.txt" in names and "checksums.sha256" in names
        assert "2026-10-01/091501_1_photo.jpg" in names  # местное время в имени
        assert "часть 1 из 2" in zf.read("README.txt").decode()
        rows = _registry(zf)
        checksums = zf.read("checksums.sha256").decode()
    assert checksums == f"{photo1.file_sha256}  2026-10-01/091501_1_photo.jpg\n"
    # Реестр полный в каждой части: заголовок + 3 записи, включая текстовую
    assert len(rows) == 4
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["1"][6] == "Армирование" and by_id["1"][12] == "совпадает"
    assert by_id["2"][12] == "ХЕШ НЕ СОВПАДАЕТ"
    assert by_id["3"][5] == "текст" and by_id["3"][9] == ""


def test_build_archive_too_large(tmp_path):
    storage = LocalFileStorage(tmp_path / "media")
    entry = _entry(1, EntryKind.PHOTO, storage, b"a" * 2000)
    with pytest.raises(ArchiveTooLarge):
        build_archive([entry], storage, "UTC", tmp_path, "a", "Склад", "01.10.2026", max_total=1000)


async def test_archive_flow(sessionmaker, settings, bot, tg, tmp_path):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot, "Склад")
    await dp.feed_update(bot, make_update(text="Привезли газоблок"))
    photo = [{"file_id": "photo-file", "file_unique_id": "p1", "width": 10, "height": 10}]
    await dp.feed_update(bot, make_update(photo=photo, caption="Разгрузка"))

    storage = LocalFileStorage(tmp_path / "media")
    entries = EntryQueue(sessionmaker, EntryProcessor(bot, storage, FakeTranscriber()))
    for entry_id in await entries.claim(10):
        await entries.handle(entry_id)

    await dp.feed_update(bot, make_update(text="/archive"))
    assert "за какой период" in tg.sent_texts()[-1]
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))

    await dp.feed_update(
        bot, callback_update(ArchivePeriod(site_id=site.id, period="today").pack())
    )
    assert "⏳ Собираю архив «Склад»" in tg.sent_texts()[-1]
    # Повторное нажатие не ставит вторую тяжёлую выгрузку
    await dp.feed_update(
        bot, callback_update(ArchivePeriod(site_id=site.id, period="today").pack())
    )
    assert "уже собирается" in tg.sent_texts()[-1]

    exports = ExportQueue(sessionmaker, bot, storage)
    [job_id] = await exports.claim(10)
    await exports.handle(job_id)

    [(filename, data)] = tg.documents
    assert filename.startswith(f"archive_{site.id}_") and filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        rows = _registry(zf)
        media = [n for n in zf.namelist() if n.endswith(".jpg")]
        assert len(media) == 1 and zf.read(media[0]) == b"jpeg-bytes"
    assert [r[5] for r in rows[1:]] == ["текст", "фото"]
    assert rows[2][6] == "Разгрузка" and rows[2][12] == "совпадает"

    async with sessionmaker() as s:
        job = await s.get(ExportJob, job_id)
    assert job.status == "done" and job.files_count == 1
