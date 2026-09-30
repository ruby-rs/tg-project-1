import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredFile:
    key: str
    size: int
    sha256: str


class LocalFileStorage:
    """Архив медиа на диске сервера.

    Файлы только добавляются и никогда не перезаписываются: вместе с SHA-256
    в БД это позволяет подтвердить подлинность фото в споре с заказчиком.
    Структура: {company_id}/{ГГГГ}/{ММ}/{ДД}/{ЧЧММСС}_{entry_id}{ext}
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def build_key(company_id: int, local_dt: datetime, entry_id: int, ext: str) -> str:
        return f"{company_id}/{local_dt:%Y/%m/%d}/{local_dt:%H%M%S}_{entry_id}{ext}"

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Недопустимый ключ файла: {key}")
        return path

    async def save(self, key: str, data: bytes) -> StoredFile:
        return await asyncio.to_thread(self._save_sync, key, data)

    def _save_sync(self, key: str, data: bytes) -> StoredFile:
        digest = hashlib.sha256(data).hexdigest()
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Повторная попытка обработки: файл уже сохранён ранее
            existing = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing != digest:
                raise FileExistsError(f"{key} уже существует с другим содержимым")
            return StoredFile(key, len(data), digest)

        tmp = path.with_name(path.name + ".part")
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return StoredFile(key, len(data), digest)
