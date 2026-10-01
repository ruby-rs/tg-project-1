import secrets
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import (
    Company,
    DailyReport,
    Entry,
    EntryStatus,
    Site,
    User,
    UserRole,
)


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, tg_id: int, full_name: str, username: str | None) -> User:
        user = await self.session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            user = User(tg_id=tg_id, full_name=full_name, username=username)
            self.session.add(user)
            await self.session.flush()
        elif user.full_name != full_name or user.username != username:
            user.full_name = full_name
            user.username = username
        return user

    async def list_managers(self, company_id: int) -> list[User]:
        result = await self.session.scalars(
            select(User).where(
                User.company_id == company_id,
                User.role.in_([UserRole.OWNER, UserRole.MANAGER]),
            )
        )
        return list(result)


class CompanyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, name: str, timezone: str, owner: User) -> Company:
        company = Company(name=name, timezone=timezone, invite_code=secrets.token_urlsafe(9))
        self.session.add(company)
        await self.session.flush()
        owner.company = company
        owner.role = UserRole.OWNER
        return company

    async def get_by_invite(self, code: str) -> Company | None:
        return await self.session.scalar(select(Company).where(Company.invite_code == code))


class SiteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, company_id: int, name: str) -> Site:
        site = Site(company_id=company_id, name=name)
        self.session.add(site)
        await self.session.flush()
        return site

    async def get(self, company_id: int, site_id: int) -> Site | None:
        return await self.session.scalar(
            select(Site).where(Site.id == site_id, Site.company_id == company_id)
        )

    async def get_by_name(self, company_id: int, name: str) -> Site | None:
        return await self.session.scalar(
            select(Site).where(Site.company_id == company_id, func.lower(Site.name) == name.lower())
        )

    async def list_active(self, company_id: int) -> list[Site]:
        result = await self.session.scalars(
            select(Site)
            .where(Site.company_id == company_id, Site.is_active.is_(True))
            .order_by(Site.name)
        )
        return list(result)


class EntryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: Entry) -> bool:
        """Сохраняет сообщение. False — дубль (Telegram переотправил апдейт)."""
        values = {
            c.key: getattr(entry, c.key)
            for c in Entry.__table__.columns
            if getattr(entry, c.key, None) is not None
        }
        stmt = (
            pg_insert(Entry)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_entries_tg_message")
            .returning(Entry.id)
        )
        entry_id = await self.session.scalar(stmt)
        if entry_id is None:
            return False
        entry.id = entry_id
        return True

    async def get_by_message(self, chat_id: int, message_id: int) -> Entry | None:
        return await self.session.scalar(
            select(Entry).where(Entry.tg_chat_id == chat_id, Entry.tg_message_id == message_id)
        )

    async def get_by_transcript_message(self, chat_id: int, message_id: int) -> Entry | None:
        return await self.session.scalar(
            select(Entry).where(
                Entry.tg_chat_id == chat_id, Entry.transcript_message_id == message_id
            )
        )

    async def album_caption(self, chat_id: int, media_group_id: str) -> str | None:
        """Подпись альбома: Telegram кладёт её только в одно из сообщений группы."""
        return await self.session.scalar(
            select(Entry.text)
            .where(
                Entry.tg_chat_id == chat_id,
                Entry.media_group_id == media_group_id,
                Entry.text.is_not(None),
            )
            .order_by(Entry.tg_message_id)
            .limit(1)
        )

    async def assign_unsorted(self, user_id: int, site_id: int) -> int:
        """Привязывает к объекту сообщения, присланные до выбора объекта."""
        result = await self.session.execute(
            update(Entry)
            .where(Entry.user_id == user_id, Entry.site_id.is_(None))
            .values(site_id=site_id)
        )
        return result.rowcount or 0

    async def for_report(self, site_id: int, work_date: date) -> list[Entry]:
        result = await self.session.scalars(
            select(Entry)
            .options(joinedload(Entry.user))
            .where(
                Entry.site_id == site_id,
                Entry.work_date == work_date,
                Entry.status == EntryStatus.DONE,
            )
            .order_by(Entry.sent_at, Entry.id)
        )
        return list(result)

    async def count_unprocessed(self, site_id: int, work_date: date) -> int:
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(Entry)
                .where(
                    Entry.site_id == site_id,
                    Entry.work_date == work_date,
                    Entry.status.in_([EntryStatus.PENDING, EntryStatus.PROCESSING]),
                )
            )
            or 0
        )

    async def claim_batch(self, limit: int, stale_after: int) -> list[int]:
        """Забирает задачи в работу. SKIP LOCKED позволяет запускать несколько воркеров."""
        ready = and_(
            Entry.status == EntryStatus.PENDING,
            or_(Entry.next_attempt_at.is_(None), Entry.next_attempt_at <= func.now()),
        )
        stale = and_(
            Entry.status == EntryStatus.PROCESSING,
            Entry.locked_at < func.now() - timedelta(seconds=stale_after),
        )
        candidates = (
            select(Entry.id)
            .where(or_(ready, stale))
            .order_by(Entry.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self.session.scalars(
            update(Entry)
            .where(Entry.id.in_(candidates))
            .values(
                status=EntryStatus.PROCESSING,
                locked_at=func.now(),
                attempts=Entry.attempts + 1,
            )
            .returning(Entry.id)
            .execution_options(synchronize_session=False)
        )
        return list(result)


class ReportRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, site_id: int, work_date: date, data: dict, model: str, entries_count: int
    ) -> None:
        stmt = pg_insert(DailyReport).values(
            site_id=site_id,
            work_date=work_date,
            data=data,
            model=model,
            entries_count=entries_count,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_daily_reports_site_date",
            set_={
                "data": stmt.excluded.data,
                "model": stmt.excluded.model,
                "entries_count": stmt.excluded.entries_count,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)
