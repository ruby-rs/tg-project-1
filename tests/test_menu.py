"""Главное меню кнопками и сборник медиа к отчёту."""

from datetime import UTC, date, datetime, time

import pytest
from aiogram.methods import SendDocument, SendMediaGroup, SendMessage, SendPhoto
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, ReplyKeyboardMarkup
from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.handlers.settings import OFF_VALUE, SettingsAction
from app.bot.keyboards import Cancel, Menu, ReportMedia, ReportMenu, ReportSite, SiteAdmin
from app.db.models import Company, Entry, EntryKind, ReportJob, Site, User
from app.services import media as media_service
from app.services.media import collect_media, send_media_collection
from app.timeutils import today_for
from tests.helpers import TG_USER_ID, callback_update, make_update, register_owner_with_site
from tests.test_admin import FOREMAN_ID, join_foreman_to_site


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(media_service, "PAUSE", 0)


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _menu_buttons(markup: ReplyKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


async def test_menu_follows_role_and_is_not_saved_as_entry(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    owner_menu = next(
        r.reply_markup
        for r in tg.requests
        if isinstance(r, SendMessage) and isinstance(r.reply_markup, ReplyKeyboardMarkup)
    )
    assert set(_menu_buttons(owner_menu)) == set(Menu.ALL)

    await join_foreman_to_site(dp, bot, sessionmaker)
    foreman_menu = [
        r.reply_markup
        for r in tg.requests
        if isinstance(r, SendMessage)
        and r.chat_id == FOREMAN_ID
        and isinstance(r.reply_markup, ReplyKeyboardMarkup)
    ][-1]
    assert _menu_buttons(foreman_menu) == list(Menu.FOREMAN)

    # Кнопка руководителя у прораба (меню от прежней роли) — отказ и новое меню
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text=Menu.TEAM))
    assert "только руководителю" in tg.sent_texts()[-1]

    # Нажатия кнопок не попадают в отчёт как сообщения с объекта
    for text in Menu.ALL:
        await dp.feed_update(bot, make_update(text=text))
    async with sessionmaker() as s:
        assert await s.scalar(select(Entry)) is None


async def test_report_by_buttons(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await register_site(dp, bot, "Склад")
    async with sessionmaker() as s:
        site = await s.scalar(select(Site).where(Site.name == "Склад"))

    # У руководителя два объекта: сначала выбор объекта, потом дня
    tg.requests.clear()
    await dp.feed_update(bot, make_update(text=Menu.REPORT))
    assert tg.sent_texts()[-1] == "📋 По какому объекту отчёт?"
    assert "🏗 Склад" in _buttons(tg.requests[-1].reply_markup)

    await dp.feed_update(bot, callback_update(ReportMenu(site_id=site.id).pack()))
    assert "Склад" in tg.sent_texts()[-1]
    days = _buttons(tg.requests[-1].reply_markup)
    assert days == ["Сегодня", "Вчера", "Позавчера", "🏗 Другой объект"]

    await dp.feed_update(bot, callback_update(ReportSite(site_id=site.id, days_ago=1).pack()))
    assert "⏳ Формирую отчёт по «Склад»" in tg.sent_texts()[-1]
    async with sessionmaker() as s:
        job = await s.scalar(select(ReportJob))
    assert job.site_id == site.id
    today = today_for(settings.default_timezone, settings.work_day_start_hour)
    assert (today - job.work_date).days == 1


async def register_site(dp, bot, name: str) -> None:
    await dp.feed_update(bot, make_update(text=Menu.SITES))
    await dp.feed_update(bot, callback_update("newsite"))
    await dp.feed_update(bot, make_update(text=name))


async def test_menu_button_interrupts_dialog(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))

    # Начали переименование и передумали — нажали кнопку меню
    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    assert _buttons(tg.requests[-1].reply_markup) == ["✖️ Отмена"]
    await dp.feed_update(bot, make_update(text=Menu.REPORT))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).name == "ЖК Северный"

    # Или нажали «Отмена» под вопросом
    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    await dp.feed_update(bot, callback_update(Cancel().pack()))
    await dp.feed_update(bot, make_update(text="Новое имя"))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).name == "ЖК Северный"
        assert await s.scalar(select(Entry.text)) == "Новое имя"  # обычное сообщение


async def test_settings_time_by_buttons(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, make_update(text=Menu.SETTINGS))
    await dp.feed_update(bot, callback_update(SettingsAction(action="digest").pack()))
    assert "20:00" in _buttons(tg.requests[-1].reply_markup)
    await dp.feed_update(bot, callback_update(SettingsAction(action="dset", value=20 * 60).pack()))
    await dp.feed_update(bot, callback_update(SettingsAction(action="reminder").pack()))
    off = SettingsAction(action="rset", value=OFF_VALUE).pack()
    await dp.feed_update(bot, callback_update(off))
    async with sessionmaker() as s:
        company = await s.scalar(select(Company))
    assert company.digest_time == time(20, 0)
    assert company.reminder_time is None


def _entry(n: int, kind: EntryKind, **kw) -> Entry:
    kw.setdefault("tg_file_id", f"file-{n}")
    return Entry(
        id=n,
        kind=kind,
        sent_at=datetime(2026, 10, 8, 9, n % 60, tzinfo=UTC),
        user=User(full_name="Иван Петров"),
        **kw,
    )


async def test_media_collection_albums(bot, tg):
    entries = [_entry(i, EntryKind.PHOTO) for i in range(12)]
    entries += [
        _entry(20, EntryKind.VIDEO, text="Заливка <бетона>"),
        _entry(21, EntryKind.PHOTO, file_name="IMG_1.jpg", mime_type="image/jpeg"),
        _entry(22, EntryKind.DOCUMENT, file_name="акт.pdf"),
        _entry(23, EntryKind.VOICE),  # голосовые — в отчёте, не в сборнике
        _entry(24, EntryKind.TEXT, tg_file_id=None, text="текст"),
    ]
    assert collect_media(entries).total == 15

    sent = await send_media_collection(
        bot, TG_USER_ID, entries, "ЖК Северный", date(2026, 10, 8), "Europe/Moscow"
    )
    assert sent == 15
    title = tg.sent_texts()[0]
    assert "фото 12, видео 1, файлов 2" in title

    albums = [r for r in tg.requests if isinstance(r, SendMediaGroup)]
    assert [len(a.media) for a in albums] == [10, 3, 2]
    assert all(isinstance(m, InputMediaPhoto) for m in albums[0].media)
    video = albums[1].media[-1]
    assert isinstance(video, InputMediaVideo)
    assert video.caption == "12:20 · Иван Петров — Заливка &lt;бетона&gt;"
    # Фото «файлом» уходит документом — оригинал с EXIF
    assert all(isinstance(m, InputMediaDocument) for m in albums[2].media)


async def test_media_single_file_is_sent_without_album(bot, tg):
    await send_media_collection(
        bot,
        TG_USER_ID,
        [_entry(1, EntryKind.PHOTO), _entry(2, EntryKind.DOCUMENT, file_name="a.pdf")],
        "Склад",
        date(2026, 10, 8),
        "UTC",
    )
    assert not [r for r in tg.requests if isinstance(r, SendMediaGroup)]
    assert [r.photo for r in tg.requests if isinstance(r, SendPhoto)] == ["file-1"]
    assert [r.document for r in tg.requests if isinstance(r, SendDocument)] == ["file-2"]


async def test_media_button_under_report(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    photo = [{"file_id": "photo-file", "file_unique_id": "p1", "width": 10, "height": 10}]
    await dp.feed_update(bot, make_update(photo=photo, caption="Опалубка"))
    async with sessionmaker() as s:
        entry = await s.scalar(select(Entry))
        entry.status = "done"
        await s.commit()

    tg.requests.clear()
    day = entry.work_date.toordinal()
    await dp.feed_update(bot, callback_update(ReportMedia(site_id=entry.site_id, day=day).pack()))
    assert tg.sent_texts()[0].startswith("📸 <b>Медиа за")
    [sent] = [r for r in tg.requests if isinstance(r, SendPhoto)]
    assert sent.photo == "photo-file" and "Опалубка" in sent.caption
