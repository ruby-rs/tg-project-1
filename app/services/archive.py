"""Выгрузка архива объекта: файлы, реестр и контрольные суммы для споров с заказчиком.

Сборка синхронная (файловый ввод-вывод) — вызывать через asyncio.to_thread.
"""

import csv
import hashlib
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.db.models import Entry, EntryKind
from app.services.storage import LocalFileStorage
from app.timeutils import to_local

PART_LIMIT = 45 * 1024 * 1024  # Bot API принимает документы до 50 МБ
MAX_TOTAL = 400 * 1024 * 1024  # больше — просим выбрать период короче

KIND_TITLES = {
    EntryKind.TEXT: "текст",
    EntryKind.VOICE: "голосовое",
    EntryKind.AUDIO: "аудио",
    EntryKind.VIDEO_NOTE: "видеокружок",
    EntryKind.PHOTO: "фото",
    EntryKind.VIDEO: "видео",
    EntryKind.DOCUMENT: "файл",
}

REGISTRY_HEADER = [
    "№",
    "Дата работ",
    "Отправлено",
    "Снято (EXIF)",
    "Автор",
    "Тип",
    "Текст / подпись",
    "Расшифровка",
    "Описание фото",
    "Файл в архиве",
    "Размер, байт",
    "SHA-256",
    "Проверка файла",
]

README = """\
Архив материалов объекта «{site}» за {period}.

Состав:
  ГГГГ-ММ-ДД/       — фото, видео и голосовые по дням (время в имени — местное)
  registry.csv      — реестр всех сообщений за период: кто, когда, что прислал,
                      расшифровки, подписи, SHA-256 файлов (открывается в Excel)
  checksums.sha256  — контрольные суммы файлов этой части архива

Контрольная сумма SHA-256 каждого файла вычислена и сохранена в системе в момент
получения файла от прораба. При выгрузке файлы сверены с ней — результат в столбце
«Проверка файла» реестра. Совпадение суммы подтверждает, что файл не изменялся.

Как проверить самостоятельно:
  Windows:       certutil -hashfile "путь\\к\\файлу.jpg" SHA256
  Linux / macOS: sha256sum -c checksums.sha256
{parts_note}"""


@dataclass(slots=True)
class ArchiveItem:
    entry: Entry
    member: str | None  # путь внутри ZIP; None — у записи нет файла
    source: Path | None
    size: int
    check: str  # результат сверки SHA-256


@dataclass(slots=True)
class ArchiveResult:
    parts: list[Path]
    files: int
    total_size: int


class ArchiveTooLarge(Exception):
    def __init__(self, total_size: int) -> None:
        super().__init__(total_size)
        self.total_size = total_size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_items(
    entries: Sequence[Entry], storage: LocalFileStorage, tz_name: str
) -> list[ArchiveItem]:
    items = []
    for e in entries:
        if not e.file_path:
            items.append(ArchiveItem(e, None, None, 0, ""))
            continue
        path = storage.path(e.file_path)
        local = to_local(e.sent_at, tz_name)
        ext = Path(e.file_path).suffix
        member = f"{local:%Y-%m-%d}/{local:%H%M%S}_{e.id}_{e.kind}{ext}"
        if not path.exists():
            items.append(ArchiveItem(e, None, None, 0, "файл отсутствует"))
            continue
        actual = _sha256(path)
        check = "совпадает" if actual == e.file_sha256 else "ХЕШ НЕ СОВПАДАЕТ"
        items.append(ArchiveItem(e, member, path, path.stat().st_size, check))
    return items


def registry_csv(items: Sequence[ArchiveItem], tz_name: str) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(REGISTRY_HEADER)
    for item in items:
        e = item.entry
        writer.writerow(
            [
                e.id,
                f"{e.work_date:%d.%m.%Y}",
                f"{to_local(e.sent_at, tz_name):%d.%m.%Y %H:%M:%S}",
                f"{to_local(e.taken_at, tz_name):%d.%m.%Y %H:%M:%S}" if e.taken_at else "",
                e.user.full_name if e.user else "",
                KIND_TITLES.get(e.kind, e.kind),
                e.text or "",
                e.transcript or "",
                e.photo_description or "",
                item.member or "",
                item.size or "",
                e.file_sha256 or "",
                item.check,
            ]
        )
    # BOM — чтобы Excel сразу открыл кириллицу
    return buf.getvalue().encode("utf-8-sig")


def split_parts(items: Sequence[ArchiveItem], limit: int) -> list[list[ArchiveItem]]:
    parts: list[list[ArchiveItem]] = [[]]
    size = 0
    for item in (i for i in items if i.source is not None):
        if parts[-1] and size + item.size > limit:
            parts.append([])
            size = 0
        parts[-1].append(item)
        size += item.size
    return parts


def build_archive(
    entries: Sequence[Entry],
    storage: LocalFileStorage,
    tz_name: str,
    out_dir: Path,
    base_name: str,
    site_name: str,
    period: str,
    *,
    part_limit: int = PART_LIMIT,
    max_total: int = MAX_TOTAL,
) -> ArchiveResult:
    items = collect_items(entries, storage, tz_name)
    total = sum(i.size for i in items)
    if total > max_total:
        raise ArchiveTooLarge(total)

    registry = registry_csv(items, tz_name)
    groups = split_parts(items, part_limit)
    paths = []
    for n, group in enumerate(groups, start=1):
        suffix = f"_part{n}" if len(groups) > 1 else ""
        path = out_dir / f"{base_name}{suffix}.zip"
        parts_note = (
            f"\nЭто часть {n} из {len(groups)}. Реестр в каждой части — полный.\n"
            if len(groups) > 1
            else ""
        )
        readme = README.format(site=site_name, period=period, parts_note=parts_note)
        checksums = "".join(
            f"{i.entry.file_sha256}  {i.member}\n" for i in group if i.entry.file_sha256
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("README.txt", readme, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("registry.csv", registry, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("checksums.sha256", checksums, compress_type=zipfile.ZIP_DEFLATED)
            for item in group:
                # Фото, видео и голосовые уже сжаты — храним как есть, без нагрузки на CPU
                zf.write(item.source, item.member, compress_type=zipfile.ZIP_STORED)
        paths.append(path)
    files = sum(1 for i in items if i.source is not None)
    return ArchiveResult(paths, files, total)
