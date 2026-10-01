import os
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import get_routers
from app.config import Settings
from app.db.models import Base
from app.db.session import create_engine, create_sessionmaker
from tests.helpers import MockedSession

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:TEST",
        llm_model="test-model",
        media_root=tmp_path / "media",
        database_url=TEST_DATABASE_URL or "postgresql+asyncpg://localhost/none",
    )


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL не задан — интеграционные тесты пропущены")
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield create_sessionmaker(engine)
    async with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture(autouse=True)
def _detach_routers():
    """Роутеры aiogram — синглтоны модулей и крепятся к одному диспетчеру.

    В тестах диспетчер создаётся заново, поэтому отвязываем их после каждого теста.
    """
    yield
    for router in get_routers():
        router._parent_router = None


@pytest.fixture
def tg() -> MockedSession:
    return MockedSession(files={"voice-file": b"OggS-fake-voice", "photo-file": b"jpeg-bytes"})


@pytest.fixture
def bot(tg: MockedSession) -> Bot:
    return Bot("123456:TEST", session=tg)
