"""Меню по команде /menu с переходами в одном сообщении и сборник медиа к отчёту."""

from datetime import UTC, date, datetime, time

import pytest
from aiogram.methods import (
    DeleteMessage,
    EditMessageText,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SetMyCommands,
)
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo
from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.commands import set_default_commands
from app.bot.handlers.settings import OFF_VALUE, SettingsAction
from app.bot.keyboards import Cancel, Menu, Nav, ReportMedia, ReportMenu, ReportSite, SiteAdmin
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


def _sent(tg) -> list[SendMessage]:
    return [r for r in tg.requests if isinstance(r, SendMessage)]


async def test_only_menu_command_is_visible(bot, tg):
    await set_default_commands(bot)
    [cmds] = [r for r in tg.requests if isinstance(r, SetMyCommands)]
    assert [c.command for c in cmds.commands] == ["menu"]


async def test_menu_follows_role(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    # Личный список команд руководителя убран: у всех видна только /menu
    assert not [r for r in tg.requests if isinstance(r, SetMyCommands)]

    tg.requests.clear()
    await dp.feed_update(bot, make_update(text="/menu"))
    # Сообщение «/menu» удаляется, меню приходит одним сообщением с кнопками
    assert any(isinstance(r, DeleteMessage) and r.chat_id == TG_USER_ID for r in tg.requests)
    menu = _sent(tg)[-1]
    assert menu.text.startswith("☰ <b>Меню</b>")
    assert "📍 Текущий объект: ЖК Северный" in menu.text
    assert _buttons(menu.reply_markup) == [
        Menu.REPORT,
        Menu.STATS,
        Menu.SITES,
        Menu.TEAM,
        Menu.INVITE,
        Menu.SETTINGS,
        Menu.ARCHIVE,
        Menu.SITE,
        Menu.HELP,
    ]

    await join_foreman_to_site(dp, bot, sessionmaker)
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="/menu"))
    assert _buttons(_sent(tg)[-1].reply_markup) == [
        Menu.REPORT,
        Menu.SITE,
        Menu.ARCHIVE,
        Menu.HELP,
    ]
    # Раздел руководителя по старой кнопке — отказ, без новых сообщений
    await dp.feed_update(bot, callback_update(Nav(to="team").pack(), user_id=FOREMAN_ID))
    assert "только руководителю" in tg.sent_texts()[-1]


async def test_navigation_edits_one_message(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await register_site(dp, bot, "Склад")
    async with sessionmaker() as s:
        site = await s.scalar(select(Site).where(Site.name == "Склад"))

    tg.requests.clear()
    # У руководителя два объекта: объект → день → отчёт поставлен в очередь
    await dp.feed_update(bot, callback_update(Nav(to="report").pack()))
    assert tg.sent_texts()[-1] == "📋 По какому объекту отчёт?"
    assert "🏗 Склад" in _buttons(tg.requests[-1].reply_markup)
    await dp.feed_update(bot, callback_update(ReportMenu(site_id=site.id).pack()))
    days = _buttons(tg.requests[-1].reply_markup)
    assert days == ["Сегодня", "Вчера", "Позавчера", "🏗 Другой объект", "← Меню"]
    await dp.feed_update(bot, callback_update(ReportSite(site_id=site.id, days_ago=1).pack()))
    assert "⏳ Формирую отчёт по «Склад»" in tg.sent_texts()[-1]

    # Переименование: вопрос в том же сообщении, ответ пользователя удаляется
    await dp.feed_update(bot, callback_update(Nav(to="sites").pack()))
    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    rename = make_update(text="Склад №2")
    await dp.feed_update(bot, rename)
    assert any(
        isinstance(r, DeleteMessage) and r.message_id == rename.message.message_id
        for r in tg.requests
    )
    edited = [r for r in tg.requests if isinstance(r, EditMessageText)][-1]
    assert edited.message_id == 1 and "✅ Название изменено" in edited.text

    # Ни одного нового сообщения — всё правками
    assert _sent(tg) == []
    async with sessionmaker() as s:
        job = await s.scalar(select(ReportJob))
        assert (await s.get(Site, site.id)).name == "Склад №2"
    today = today_for(settings.default_timezone, settings.work_day_start_hour)
    assert job.site_id == site.id and (today - job.work_date).days == 1


async def register_site(dp, bot, name: str) -> None:
    await dp.feed_update(bot, callback_update("newsite"))
    await dp.feed_update(bot, make_update(text=name))


async def test_menu_interrupts_dialog_and_old_buttons(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))

    # Начали переименование и передумали — открыли меню
    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    assert _buttons(tg.requests[-1].reply_markup) == ["✖️ Отмена"]
    await dp.feed_update(bot, make_update(text="/menu"))
    # Или нажали «Отмена» — сообщение с вопросом снова становится меню
    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    await dp.feed_update(bot, callback_update(Cancel().pack()))
    assert tg.sent_texts()[-1].startswith("☰ <b>Меню</b>")

    # Кнопки прежнего меню под полем ввода открывают новое и не сохраняются как сообщения
    for text in Menu.LEGACY:
        await dp.feed_update(bot, make_update(text=text))
    await dp.feed_update(bot, make_update(text="Новое имя"))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).name == "ЖК Северный"
        assert list(await s.scalars(select(Entry.text))) == ["Новое имя"]


async def test_settings_time_by_buttons(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await dp.feed_update(bot, callback_update(Nav(to="settings").pack()))
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
