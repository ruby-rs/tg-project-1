"""Управление объектами (/sites) и командой (/team)."""

from sqlalchemy import select

from app.bot.app import build_dispatcher
from app.bot.handlers.admin import SiteAdmin, TeamAdmin
from app.db.models import Site, SiteMember, User, UserRole
from app.db.repositories import SiteRepo
from tests.helpers import TG_USER_ID, callback_update, make_update, register_owner_with_site

FOREMAN_ID = 777


async def join_foreman_to_site(dp, bot, sessionmaker, site_name: str = "ЖК Северный") -> None:
    async with sessionmaker() as s:
        site = await s.scalar(select(Site).where(Site.name == site_name))
    await dp.feed_update(
        bot, make_update(user_id=FOREMAN_ID, text=f"/start site_{site.invite_code}")
    )
    await dp.feed_update(bot, callback_update("consent:accept", user_id=FOREMAN_ID))


async def test_site_rename_address_close(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))

    await dp.feed_update(bot, callback_update(SiteAdmin(action="rename", site_id=site.id).pack()))
    await dp.feed_update(bot, make_update(text="ЖК Южный"))
    assert "Название изменено" in tg.sent_texts()[-1]

    await dp.feed_update(bot, callback_update(SiteAdmin(action="address", site_id=site.id).pack()))
    await dp.feed_update(bot, make_update(text="г. Казань, ул. Строителей, 5"))
    assert "Адрес: г. Казань, ул. Строителей, 5" in tg.sent_texts()[-1]

    await dp.feed_update(bot, callback_update(SiteAdmin(action="close", site_id=site.id).pack()))
    async with sessionmaker() as s:
        site = await s.get(Site, site.id)
        owner = await s.scalar(select(User))
        assert site.name == "ЖК Южный" and site.is_active is False
        assert owner.current_site_id is None  # на закрытый объект сообщения не идут
        assert await SiteRepo(s).list_for_user(owner) == []

    await dp.feed_update(bot, callback_update(SiteAdmin(action="open", site_id=site.id).pack()))
    async with sessionmaker() as s:
        assert (await s.get(Site, site.id)).is_active is True


async def test_kick_foreman_from_site(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await join_foreman_to_site(dp, bot, sessionmaker)
    async with sessionmaker() as s:
        site = await s.scalar(select(Site))
        foreman = await s.scalar(select(User).where(User.tg_id == FOREMAN_ID))
    assert foreman.current_site_id == site.id

    kick = SiteAdmin(action="kick", site_id=site.id, user_id=foreman.id).pack()
    await dp.feed_update(bot, callback_update(kick))

    async with sessionmaker() as s:
        foreman = await s.get(User, foreman.id)
        assert foreman.current_site_id is None
        assert await s.scalar(select(SiteMember).where(SiteMember.user_id == foreman.id)) is None
    notices = [r for r in tg.requests if getattr(r, "chat_id", None) == FOREMAN_ID]
    assert "Вас убрали с объекта" in notices[-1].text


async def test_owner_manages_team(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await join_foreman_to_site(dp, bot, sessionmaker)
    async with sessionmaker() as s:
        foreman = await s.scalar(select(User).where(User.tg_id == FOREMAN_ID))

    await dp.feed_update(
        bot, callback_update(TeamAdmin(action="promote", user_id=foreman.id).pack())
    )
    async with sessionmaker() as s:
        assert (await s.get(User, foreman.id)).role == UserRole.MANAGER

    # Новый руководитель не может менять роли и не может удалить владельца
    async with sessionmaker() as s:
        owner = await s.scalar(select(User).where(User.tg_id == TG_USER_ID))
    tg.requests.clear()
    await dp.feed_update(
        bot, callback_update(TeamAdmin(action="remove_ok", user_id=owner.id).pack(), FOREMAN_ID)
    )
    assert any(getattr(r, "text", None) == "Недостаточно прав" for r in tg.requests)

    await dp.feed_update(
        bot, callback_update(TeamAdmin(action="remove_ok", user_id=foreman.id).pack())
    )
    async with sessionmaker() as s:
        removed = await s.get(User, foreman.id)
        assert removed.company_id is None and removed.role == UserRole.FOREMAN
        assert await s.scalar(select(SiteMember).where(SiteMember.user_id == foreman.id)) is None


async def test_foreman_has_no_admin_commands(sessionmaker, settings, bot, tg):
    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    await join_foreman_to_site(dp, bot, sessionmaker)
    await dp.feed_update(bot, make_update(user_id=FOREMAN_ID, text="/team"))
    assert "Не знаю такой команды" in tg.sent_texts()[-1]


async def test_command_menu_follows_role(sessionmaker, settings, bot, tg):
    from aiogram.methods import DeleteMyCommands, SetMyCommands

    dp = build_dispatcher(settings, sessionmaker)
    await register_owner_with_site(dp, bot)
    owner_menu = [r for r in tg.requests if isinstance(r, SetMyCommands)]
    assert owner_menu and owner_menu[-1].scope.chat_id == TG_USER_ID
    assert "team" in [c.command for c in owner_menu[-1].commands]

    await join_foreman_to_site(dp, bot, sessionmaker)
    async with sessionmaker() as s:
        foreman = await s.scalar(select(User).where(User.tg_id == FOREMAN_ID))

    tg.requests.clear()
    await dp.feed_update(
        bot, callback_update(TeamAdmin(action="promote", user_id=foreman.id).pack())
    )
    [menu] = [r for r in tg.requests if isinstance(r, SetMyCommands)]
    assert menu.scope.chat_id == FOREMAN_ID

    tg.requests.clear()
    await dp.feed_update(
        bot, callback_update(TeamAdmin(action="demote", user_id=foreman.id).pack())
    )
    [reset] = [r for r in tg.requests if isinstance(r, DeleteMyCommands)]
    assert reset.scope.chat_id == FOREMAN_ID
