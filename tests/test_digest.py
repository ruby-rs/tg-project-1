"""Вечерняя сводка, напоминания и настройки компании."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.handlers.settings import SettingsAction, parse_time
from app.bot.keyboards import ReportView
from app.db.models import Company, Entry, ScheduledKind, ScheduledRun, Site, User
from app.reports.digest import Digest, SiteDigest, render_digest
from app.reports.schema import SiteDailyReport
from app.services.llm import LLMClient
from app.services.reports import ReportService
from app.worker.scheduled import ScheduledQueue, due_kinds, schedule_due_runs
from tests.helpers import (
    REPORT_JSON,
    callback_update,
    fake_openai,
    make_update,
    register_owner_with_site,
)

FOREMAN_ID = 777
MSK = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("19:30", time(19, 30)),
        ("9.05", time(9, 5)),
        ("1800", time(18, 0)),
        ("7", time(7, 0)),
        ("выкл", None),
        ("-", None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["25:00", "завтра", "19:61"])
def test_parse_time_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


def _company(**kw) -> Company:
    base = dict(
        timezone="Europe/Moscow",
        digest_time=time(19, 0),
        reminder_time=time(17, 0),
        work_days="12345",
    )
    base.update(kw)
    return Company(**base)


def test_due_kinds_respects_time_window_and_workdays():
    def thu(h: int, m: int = 0) -> datetime:  # четверг 1 октября 2026, время московское
        return datetime(2026, 10, 1, h, m, tzinfo=MSK)

    assert due_kinds(_company(), thu(16, 59)) == []
    assert due_kinds(_company(), thu(17, 0)) == [ScheduledKind.REMINDER]
    assert set(due_kinds(_company(), thu(19, 30))) == {ScheduledKind.REMINDER, ScheduledKind.DIGEST}
    # Окно догоняния — 3 часа: в 21:30 напоминание (17:00) уже не шлём, сводку (19:00) — да
    assert due_kinds(_company(), thu(21, 30)) == [ScheduledKind.DIGEST]
    assert due_kinds(_company(), thu(22, 30)) == []
    assert due_kinds(_company(digest_time=None, reminder_time=None), thu(19, 30)) == []
    # Суббота не рабочая: напоминания нет, сводка создаётся (уйдёт, только если были сообщения)
    sat = datetime(2026, 10, 3, 19, 30, tzinfo=MSK)
    assert due_kinds(_company(), sat) == [ScheduledKind.DIGEST]


def test_render_digest():
    report = SiteDailyReport.model_validate(
        {
            "summary": "Залили перекрытие",
            "work_done": [{"description": "Бетон", "quantity": 12, "unit": "м³"}],
            "schedule_risks": [{"description": "Нет крана", "severity": "high"}],
        }
    )
    digest = Digest(
        company_name="ООО <Стройка>",
        work_date=date(2026, 10, 1),
        sites=[SiteDigest(1, "ЖК Северный", 5, report)],
        silent=["Склад"],
    )
    text = render_digest(digest)
    assert "Сводка за 01.10.2026</b> · ООО &lt;Стройка&gt;" in text
    assert "Объектов с сообщениями: 1 из 2" in text
    assert "🔴 Нет крана" in text
    assert "😶 Нет сообщений за день: Склад" in text


async def _setup(dp, bot, sessionmaker) -> tuple[Site, User]:
    await register_owner_with_site(dp, bot, "ЖК Северный")
    await dp.feed_update(bot, make_update(text="/new_object"))
    await dp.feed_update(bot, make_update(text="Склад"))
    async with sessionmaker() as s:
        site = await s.scalar(select(Site).where(Site.name == "ЖК Северный"))
    # Прораб на объекте «ЖК Северный», сегодня ничего не присылал
    await dp.feed_update(
        bot, make_update(user_id=FOREMAN_ID, text=f"/start site_{site.invite_code}")
    )
    await dp.feed_update(bot, callback_update("consent:accept", user_id=FOREMAN_ID))
    async with sessionmaker() as s:
        foreman = await s.scalar(select(User).where(User.tg_id == FOREMAN_ID))
    return site, foreman


async def test_reminder_and_digest(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    site, foreman = await _setup(dp, bot, sessionmaker)

    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
        company.work_days = "1234567"
        await s.commit()
    # 19:30 по Москве сегодняшнего (по Москве) дня — той же даты, что и сообщения
    evening = datetime.now(MSK).replace(hour=19, minute=30, second=0, microsecond=0)

    async with sessionmaker() as s:
        assert await schedule_due_runs(s, evening) == 2
        assert await schedule_due_runs(s, evening) == 0  # повторно не создаётся
        await s.commit()

    llm_client = fake_openai([REPORT_JSON])
    queue = ScheduledQueue(sessionmaker, bot, ReportService(LLMClient(llm_client, "m")))
    async with sessionmaker() as s:
        runs = {r.kind: r.id for r in await s.scalars(select(ScheduledRun))}

    # Напоминание прорабу без сообщений
    tg.requests.clear()
    await queue.handle(runs[ScheduledKind.REMINDER])
    to_foreman = [r.text for r in tg.requests if getattr(r, "chat_id", None) == FOREMAN_ID]
    assert any("от вас ещё не было сообщений по объекту «ЖК Северный»" in t for t in to_foreman)

    # Прораб прислал сообщение — сводка по одному объекту, второй «молчит»
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="Залили 12 кубов"))
    tg.requests.clear()
    await queue.handle(runs[ScheduledKind.DIGEST])
    digest_msg = tg.requests[-1]
    assert digest_msg.chat_id != FOREMAN_ID  # сводка — руководителю
    assert "Объектов с сообщениями: 1 из 2" in digest_msg.text
    assert "😶 Нет сообщений за день: Склад" in digest_msg.text
    button = digest_msg.reply_markup.inline_keyboard[0][0]
    assert button.text == "📋 ЖК Северный"

    # Кнопка «подробно» показывает сохранённый отчёт без нового запроса к LLM
    await dp.feed_update(bot, callback_update(button.callback_data))
    assert "Бетонирование перекрытия — <b>12 м³</b>" in tg.sent_texts()[-1]
    assert len(llm_client.chat.completions.calls) == 1

    async with sessionmaker() as s:
        run = await s.get(ScheduledRun, runs[ScheduledKind.DIGEST])
        assert run.status == "done"


async def test_digest_reuses_fresh_report(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    site, _ = await _setup(dp, bot, sessionmaker)
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="Залили 12 кубов"))

    llm_client = fake_openai([REPORT_JSON, REPORT_JSON])
    service = ReportService(LLMClient(llm_client, "m"))
    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
        today = (await s.scalar(select(Entry))).work_date
        await service.build(s, site, today, company.timezone)
        await s.commit()
        s.add(ScheduledRun(company_id=company.id, kind=ScheduledKind.DIGEST, work_date=today))
        await s.commit()
        run_id = (await s.scalar(select(ScheduledRun))).id

    await ScheduledQueue(sessionmaker, bot, service).handle(run_id)
    assert len(llm_client.chat.completions.calls) == 1  # отчёт актуален — LLM не вызывали


async def test_report_view_denied_for_other_foreman(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))
    # Чужой прораб без доступа к объекту
    async with sessionmaker() as s:
        other = Company(name="Другая", invite_code="OTHER")
        s.add(other)
        await s.flush()
        s.add(Site(company_id=other.id, name="Чужой", invite_code="CHUZH"))
        await s.commit()
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="/start site_CHUZH"))
    await dp.feed_update(bot, callback_update("consent:accept", user_id=FOREMAN_ID))
    tg.requests.clear()
    view = ReportView(site_id=site.id, day=date.today().toordinal()).pack()
    await dp.feed_update(bot, callback_update(view, user_id=FOREMAN_ID))
    assert any(getattr(r, "text", None) == "Отчёт не найден" for r in tg.requests)


async def test_settings_change_time_days_and_timezone(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)

    await dp.feed_update(bot, callback_update(SettingsAction(action="digest").pack()))
    await dp.feed_update(bot, make_update(text="20:15"))
    await dp.feed_update(bot, callback_update(SettingsAction(action="reminder").pack()))
    await dp.feed_update(bot, make_update(text="выкл"))
    await dp.feed_update(bot, callback_update(SettingsAction(action="day", value=6).pack()))
    await dp.feed_update(bot, callback_update(SettingsAction(action="tzset", value=6).pack()))

    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
    assert company.digest_time == time(20, 15)
    assert company.reminder_time is None
    assert company.work_days == "12345"  # суббота выключена
    assert company.timezone == "Asia/Krasnoyarsk"
    assert "Часовой пояс: Красноярск (UTC+7)" in tg.sent_texts()[-1]
