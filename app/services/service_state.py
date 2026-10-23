"""Остановлен ли бот главным администратором — с коротким кэшем.

Бот проверяет флаг на каждое сообщение, воркер — перед каждой выборкой задач;
кэш на несколько секунд избавляет БД от лишних запросов. Процесс, который сам
меняет флаг, сбрасывает кэш сразу; другой процесс узнает об изменении не позже TTL.
"""

import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import ServiceStateRepo


class ServiceStateCache:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], ttl: float = 5.0) -> None:
        self._sessionmaker = sessionmaker
        self._ttl = ttl
        self._paused = False
        self._checked_at: float | None = None

    async def paused(self) -> bool:
        now = time.monotonic()
        if self._checked_at is None or now - self._checked_at >= self._ttl:
            async with self._sessionmaker() as session:
                self._paused = await ServiceStateRepo(session).is_paused()
            self._checked_at = now
        return self._paused

    def set(self, paused: bool) -> None:
        self._paused = paused
        self._checked_at = time.monotonic()
