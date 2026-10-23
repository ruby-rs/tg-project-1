"""Панель главного администратора: активность объектов, отключение компаний и объектов,
остановка всего бота."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.bot.app import build_dispatcher
from app.bot.handlers.superadmin import Sup
from app.bot.middlewares import PAUSED_TEXT
from app.db.models import Company, Entry, ServiceState, Site
from app.db.repositories import PlatformRepo
from app.worker.runner import run_queue
from app.worker.scheduled import schedule_due_runs
from tests.helpers import callback_update, make_update, register_owner_with_site
from tests.test_admin import FOREMAN_ID, join_foreman_to_site

ADMIN_ID = 999


@pytest.fixture
def admin_settings(settings):
    return settings.model_copy(update={"superadmin_ids": f"{ADMIN_ID}, 1000"})


async def _entries(sessionmaker) -> int:
    async with sessionmaker() as s:
        return await s.scalar(select(func.count()).select_from(Entry))


def test_superadmin_ids_parsing(settings):
    assert settings.model_copy(update={"superadmin_ids": "1, 2;3"}).superadmins == {1, 2, 3}
    assert settings.superadmins == frozenset()


async def test_admin_is_hidden_from_regular_users(sessionmaker, admin_settings, bot, tg):
    dp = build_dispatcher(admin_settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, make_update(text="/admin"))
    assert "Не знаю такой команды" in tg.sent_texts()[-1]
    # Чужая кнопка панели тоже не срабатывает
    tg.requests.clear()
    await dp.feed_update(bot, callback_update(Sup(action="stop_ok").pack()))
    async with sessionmaker() as s:
        assert (await s.get(ServiceState, 1)) is None


async def test_activity_and_company_toggle(sessionmaker, admin_settings, bot, tg):
    dp = build_dispatcher(admin_settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await join_foreman_to_site(dp, bot, sessionmaker)
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="Залили фундамент"))

    # Администратору не нужны ни компания, ни согласие
    await dp.feed_update(bot, make_update(user_id=ADMIN_ID, text="/admin"))
    home = tg.sent_texts()[-1]
    assert "Панель администратора" in home and "Бот: 🟢 работает" in home
    assert "Компаний: 1, отключено: 0" in home and "🟢 сегодня 1" in home

    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
        site = await s.scalar(select(Site))
    await dp.feed_update(
        bot, callback_update(Sup(action="company", id=company.id).pack(), ADMIN_ID)
    )
    card = tg.sent_texts()[-1]
    assert "ООО Стройка" in card and "🟢 ЖК Северный — сегодня 1, за неделю 1" in card
    assert "Владелец: Иван Петров" in card

    await dp.feed_update(bot, callback_update(Sup(action="co_off", id=company.id).pack(), ADMIN_ID))
    assert "Отключить компанию «ООО Стройка»?" in tg.sent_texts()[-1]
    tg.requests.clear()
    await dp.feed_update(
        bot, callback_update(Sup(action="co_off_ok", id=company.id).pack(), ADMIN_ID)
    )
    texts = tg.sent_texts()
    assert any("приостановил работу компании" in t for t in texts)  # руководителю
    assert "⛔ <b>отключена</b>" in texts[-1]

    # Прораб отключённой компании: сообщение не сохраняется, отвечаем один раз
    before = await _entries(sessionmaker)
    tg.requests.clear()
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="Привезли арматуру"))
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="И ещё бетон"))
    assert await _entries(sessionmaker) == before
    assert len(tg.sent_texts()) == 1 and "приостановлена" in tg.sent_texts()[0]
    # Отозвать согласие можно всегда
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="/privacy"))
    assert "Персональные данные" in tg.sent_texts()[-1]

    # Отключённой компании рассылки не создаются
    async with sessionmaker() as s:
        evening = datetime.now(UTC).replace(hour=16, minute=30)  # 19:30 по Москве
        assert await schedule_due_runs(s, evening) == 0

    await dp.feed_update(bot, callback_update(Sup(action="co_on", id=company.id).pack(), ADMIN_ID))
    assert "Статус: 🟢 работает" in tg.sent_texts()[-1]
    assert any("возобновлена" in t for t in tg.sent_texts())
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="Привезли арматуру"))
    assert await _entries(sessionmaker) == before + 1

    # Объект: закрыть и открыть кнопками из карточки
    await dp.feed_update(bot, callback_update(Sup(action="site", id=site.id).pack(), ADMIN_ID))
    assert "ЖК Северный" in tg.sent_texts()[-1]
    await dp.feed_update(bot, callback_update(Sup(action="site_off", id=site.id).pack(), ADMIN_ID))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).is_active is False
    assert "Открыть объект" in str(tg.requests[-1].reply_markup)
    await dp.feed_update(bot, callback_update(Sup(action="site_on", id=site.id).pack(), ADMIN_ID))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).is_active is True


async def test_pause_and_resume_bot(sessionmaker, admin_settings, bot, tg):
    dp = build_dispatcher(admin_settings, sessionmaker)
    await register_owner_with_site(dp, bot)

    await dp.feed_update(bot, callback_update(Sup(action="stop").pack(), ADMIN_ID))
    assert "Остановить бота для всех?" in tg.sent_texts()[-1]
    await dp.feed_update(bot, callback_update(Sup(action="stop_ok").pack(), ADMIN_ID))
    assert "Бот: ⏸ <b>остановлен</b>" in tg.sent_texts()[-1]
    assert "Запустить бота" in str(tg.requests[-1].reply_markup)

    before = await _entries(sessionmaker)
    tg.requests.clear()
    await dp.feed_update(bot, make_update(text="Залили фундамент"))
    await dp.feed_update(bot, make_update(text="/menu"))
    assert await _entries(sessionmaker) == before
    assert tg.sent_texts() == [PAUSED_TEXT]
    await dp.feed_update(bot, callback_update(Sup(action="home").pack()))  # не админ
    # Администратор продолжает работать с ботом
    await dp.feed_update(bot, make_update(user_id=ADMIN_ID, text="/admin"))
    assert "остановлен" in tg.sent_texts()[-1]

    await dp.feed_update(bot, callback_update(Sup(action="start").pack(), ADMIN_ID))
    assert "Бот: 🟢 работает" in tg.sent_texts()[-1]
    await dp.feed_update(bot, make_update(text="Залили фундамент"))
    assert await _entries(sessionmaker) == before + 1


async def test_inactive_sites(sessionmaker, admin_settings, bot, tg):
    dp = build_dispatcher(admin_settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    async with sessionmaker() as s:
        [company] = await PlatformRepo(s).activity(datetime.now(UTC))
    [site] = company.sites
    assert site.last_date is None and site.is_inactive

    await dp.feed_update(bot, callback_update(Sup(action="idle").pack(), ADMIN_ID))
    assert "⚪ ЖК Северный (ООО Стройка) — сообщений ещё не было" in tg.sent_texts()[-1]

    # Через неделю после последнего сообщения объект — «тишина»
    await dp.feed_update(bot, make_update(text="Залили фундамент"))
    async with sessionmaker() as s:
        [company] = await PlatformRepo(s).activity(datetime.now(UTC) + timedelta(days=7))
    assert company.sites[0].idle_days == 7 and company.sites[0].week_count == 0


class _Queue:
    name = "test"

    def __init__(self) -> None:
        self.claims = 0

    async def claim(self, limit: int) -> list[int]:
        self.claims += 1
        return []

    async def handle(self, job_id: int) -> None:  # pragma: no cover
        pass


class _Gate:
    def __init__(self, paused: bool) -> None:
        self.value = paused

    async def paused(self) -> bool:
        return self.value


@pytest.mark.parametrize("paused", [True, False])
async def test_worker_queue_respects_pause(paused):
    queue, stop = _Queue(), asyncio.Event()
    task = asyncio.create_task(
        run_queue(queue, stop, concurrency=1, poll_interval=0.01, gate=_Gate(paused))
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert (queue.claims == 0) is paused
