from datetime import date, timedelta

from sqlalchemy import Select, and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import (
    Company,
    DailyReport,
    Entry,
    EntryStatus,
    ReportJob,
    Site,
    SiteMember,
    User,
    UserRole,
    new_invite_code,
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

    async def list_company(self, company_id: int) -> list[User]:
        role_order = case(
            (User.role == UserRole.OWNER, 0), (User.role == UserRole.MANAGER, 1), else_=2
        )
        result = await self.session.scalars(
            select(User).where(User.company_id == company_id).order_by(role_order, User.full_name)
        )
        return list(result)

    async def get_in_company(self, company_id: int, user_id: int) -> User | None:
        return await self.session.scalar(
            select(User).where(User.company_id == company_id, User.id == user_id)
        )

    async def remove_from_company(self, user: User) -> None:
        """Сообщения уходят в архив компании и остаются в отчётах; доступ закрывается."""
        await self.session.execute(delete(SiteMember).where(SiteMember.user_id == user.id))
        user.company = None
        user.current_site = None
        user.role = UserRole.FOREMAN


class CompanyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, name: str, timezone: str, owner: User) -> Company:
        company = Company(name=name, timezone=timezone, invite_code=new_invite_code())
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

    async def create(self, company_id: int, name: str, creator: User) -> Site:
        site = Site(company_id=company_id, name=name)
        self.session.add(site)
        await self.session.flush()
        await self.add_member(site.id, creator.id)
        return site

    async def add_member(self, site_id: int, user_id: int) -> None:
        await self.session.execute(
            pg_insert(SiteMember).values(site_id=site_id, user_id=user_id).on_conflict_do_nothing()
        )

    async def get_by_invite(self, code: str) -> Site | None:
        return await self.session.scalar(
            select(Site)
            .options(joinedload(Site.company))
            .where(Site.invite_code == code, Site.is_active.is_(True))
        )

    def _visible_to(self, user: User) -> Select[tuple[Site]]:
        """Руководитель видит все объекты компании, прораб — только свои."""
        stmt = select(Site).where(Site.company_id == user.company_id, Site.is_active.is_(True))
        if not user.is_manager:
            stmt = stmt.join(
                SiteMember, and_(SiteMember.site_id == Site.id, SiteMember.user_id == user.id)
            )
        return stmt

    async def get_for_user(self, user: User, site_id: int) -> Site | None:
        return await self.session.scalar(self._visible_to(user).where(Site.id == site_id))

    async def list_for_user(self, user: User) -> list[Site]:
        return list(await self.session.scalars(self._visible_to(user).order_by(Site.name)))

    async def get_by_name(self, company_id: int, name: str) -> Site | None:
        return await self.session.scalar(
            select(Site).where(Site.company_id == company_id, func.lower(Site.name) == name.lower())
        )

    async def get_in_company(self, company_id: int, site_id: int) -> Site | None:
        """Любой объект компании, включая закрытые, — для управления руководителем."""
        return await self.session.scalar(
            select(Site).where(Site.company_id == company_id, Site.id == site_id)
        )

    async def list_all(self, company_id: int) -> list[Site]:
        result = await self.session.scalars(
            select(Site)
            .where(Site.company_id == company_id)
            .order_by(Site.is_active.desc(), Site.name)
        )
        return list(result)

    async def members(self, site_id: int) -> list[User]:
        result = await self.session.scalars(
            select(User)
            .join(SiteMember, SiteMember.user_id == User.id)
            .where(SiteMember.site_id == site_id)
            .order_by(User.full_name)
        )
        return list(result)

    async def remove_member(self, site_id: int, user: User) -> None:
        await self.session.execute(
            delete(SiteMember).where(SiteMember.site_id == site_id, SiteMember.user_id == user.id)
        )
        if user.current_site_id == site_id:
            user.current_site = None

    async def set_active(self, site: Site, active: bool) -> None:
        site.is_active = active
        if not active:
            # Сообщения на закрытый объект больше не привязываются
            await self.session.execute(
                update(User).where(User.current_site_id == site.id).values(current_site_id=None)
            )


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
        return await claim_jobs(self.session, Entry, limit, stale_after)


async def claim_jobs(
    session: AsyncSession, model: type[Entry] | type[ReportJob], limit: int, stale_after: int
) -> list[int]:
    """Забирает задачи очереди в работу. SKIP LOCKED позволяет запускать несколько воркеров.

    Подходят готовые задачи (pending, срок повтора наступил) и «зависшие» (processing
    дольше stale_after секунд — например, воркер упал посреди обработки).
    """
    ready = and_(
        model.status == EntryStatus.PENDING,
        or_(model.next_attempt_at.is_(None), model.next_attempt_at <= func.now()),
    )
    stale = and_(
        model.status == EntryStatus.PROCESSING,
        model.locked_at < func.now() - timedelta(seconds=stale_after),
    )
    candidates = (
        select(model.id)
        .where(or_(ready, stale))
        .order_by(model.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = await session.scalars(
        update(model)
        .where(model.id.in_(candidates))
        .values(status=EntryStatus.PROCESSING, locked_at=func.now(), attempts=model.attempts + 1)
        .returning(model.id)
        .execution_options(synchronize_session=False)
    )
    return list(result)


class ReportJobRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, site_id: int, work_date: date, chat_id: int, user_id: int) -> bool:
        """Ставит отчёт в очередь. False — такой отчёт уже формируется для этого чата."""
        stmt = (
            pg_insert(ReportJob)
            .values(
                site_id=site_id,
                work_date=work_date,
                chat_id=chat_id,
                user_id=user_id,
                status=EntryStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=["site_id", "work_date", "chat_id"],
                index_where=ReportJob.status.in_([EntryStatus.PENDING, EntryStatus.PROCESSING]),
            )
            .returning(ReportJob.id)
        )
        return await self.session.scalar(stmt) is not None

    async def claim_batch(self, limit: int, stale_after: int) -> list[int]:
        return await claim_jobs(self.session, ReportJob, limit, stale_after)


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
