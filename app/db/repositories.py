from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Select, and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import (
    AUDIO_KINDS,
    Company,
    DailyReport,
    Entry,
    EntryStatus,
    ExportJob,
    ReportFeedback,
    ReportJob,
    ScheduledRun,
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
            # Апдейты обрабатываются параллельно: альбом от нового пользователя — это
            # несколько одновременных вставок, поэтому ON CONFLICT, а не add()
            await self.session.execute(
                pg_insert(User)
                .values(tg_id=tg_id, full_name=full_name, username=username, role=UserRole.FOREMAN)
                .on_conflict_do_nothing(index_elements=["tg_id"])
            )
            user = await self.session.scalar(select(User).where(User.tg_id == tg_id))
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

    async def foremen_to_remind(self, company_id: int, work_date: date) -> list[tuple[User, Site]]:
        """Прорабы с текущим объектом, которые за день не прислали ни одного сообщения."""
        sent_today = select(Entry.id).where(Entry.user_id == User.id, Entry.work_date == work_date)
        result = await self.session.execute(
            select(User, Site)
            .join(Site, Site.id == User.current_site_id)
            .where(
                User.company_id == company_id,
                User.role == UserRole.FOREMAN,
                User.consent_at.is_not(None),
                Site.is_active.is_(True),
                ~sent_today.exists(),
            )
        )
        return [(u, s) for u, s in result.all()]

    async def managers_with_consent(self, company_id: int) -> list[User]:
        return [m for m in await self.list_managers(company_id) if m.consent_at is not None]

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

    async def for_export(self, site_id: int, date_from: date, date_to: date) -> list[Entry]:
        result = await self.session.scalars(
            select(Entry)
            .options(joinedload(Entry.user))
            .where(
                Entry.site_id == site_id,
                Entry.work_date >= date_from,
                Entry.work_date <= date_to,
            )
            .order_by(Entry.sent_at, Entry.id)
        )
        return list(result)

    async def retry_failed(self, company_id: int) -> int:
        """Вернуть в очередь сообщения, обработка которых упала (Whisper был недоступен и т.п.)."""
        result = await self.session.execute(
            update(Entry)
            .where(Entry.company_id == company_id, Entry.status == EntryStatus.FAILED)
            .values(status=EntryStatus.PENDING, attempts=0, next_attempt_at=None)
        )
        return result.rowcount or 0

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
    session: AsyncSession,
    model: type[Entry] | type[ReportJob] | type[ScheduledRun] | type[ExportJob],
    limit: int,
    stale_after: int,
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

    async def enqueue(
        self, site_id: int, work_date: date, chat_id: int, user_id: int, *, rebuild: bool = False
    ) -> bool:
        """Ставит отчёт в очередь. False — такой отчёт уже формируется для этого чата.

        rebuild=True — собрать заново, даже если сообщения не менялись.
        """
        active = ReportJob.status.in_([EntryStatus.PENDING, EntryStatus.PROCESSING])
        stmt = (
            pg_insert(ReportJob)
            .values(
                site_id=site_id,
                work_date=work_date,
                chat_id=chat_id,
                user_id=user_id,
                status=EntryStatus.PENDING,
                rebuild=rebuild,
                attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=["site_id", "work_date", "chat_id"], index_where=active
            )
            .returning(ReportJob.id)
        )
        created = await self.session.scalar(stmt) is not None
        if not created and rebuild:
            # Ожидающая задача пересоберёт отчёт с учётом нового замечания
            await self.session.execute(
                update(ReportJob)
                .where(
                    ReportJob.site_id == site_id,
                    ReportJob.work_date == work_date,
                    ReportJob.chat_id == chat_id,
                    ReportJob.status == EntryStatus.PENDING,
                )
                .values(rebuild=True)
            )
        return created

    async def claim_batch(self, limit: int, stale_after: int) -> list[int]:
        return await claim_jobs(self.session, ReportJob, limit, stale_after)


class ExportJobRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self, site_id: int, date_from: date, date_to: date, chat_id: int, user_id: int
    ) -> bool:
        """Ставит выгрузку в очередь. False — по этому объекту для чата уже идёт выгрузка."""
        busy = await self.session.scalar(
            select(ExportJob.id).where(
                ExportJob.site_id == site_id,
                ExportJob.chat_id == chat_id,
                ExportJob.status.in_([EntryStatus.PENDING, EntryStatus.PROCESSING]),
            )
        )
        if busy is not None:
            return False
        self.session.add(
            ExportJob(
                site_id=site_id,
                date_from=date_from,
                date_to=date_to,
                chat_id=chat_id,
                user_id=user_id,
            )
        )
        await self.session.flush()
        return True

    async def claim_batch(self, limit: int, stale_after: int) -> list[int]:
        return await claim_jobs(self.session, ExportJob, limit, stale_after)


class ScheduledRunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, company_id: int, kind: str, work_date: date) -> bool:
        """Ставит рассылку на день. False — она уже была (или стоит в очереди)."""
        stmt = (
            pg_insert(ScheduledRun)
            .values(
                company_id=company_id,
                kind=kind,
                work_date=work_date,
                status=EntryStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(constraint="uq_scheduled_runs_day")
            .returning(ScheduledRun.id)
        )
        return await self.session.scalar(stmt) is not None

    async def claim_batch(self, limit: int, stale_after: int) -> list[int]:
        return await claim_jobs(self.session, ScheduledRun, limit, stale_after)


class ReportRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, site_id: int, work_date: date) -> DailyReport | None:
        return await self.session.scalar(
            select(DailyReport).where(
                DailyReport.site_id == site_id, DailyReport.work_date == work_date
            )
        )

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


class FeedbackRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def rate(self, site_id: int, work_date: date, user_id: int, rating: int) -> None:
        stmt = pg_insert(ReportFeedback).values(
            site_id=site_id, work_date=work_date, user_id=user_id, rating=rating
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_report_feedback_user",
            set_={"rating": stmt.excluded.rating, "updated_at": func.now()},
        )
        await self.session.execute(stmt)

    async def comment(self, site_id: int, work_date: date, user_id: int, text: str) -> None:
        await self.session.execute(
            update(ReportFeedback)
            .where(
                ReportFeedback.site_id == site_id,
                ReportFeedback.work_date == work_date,
                ReportFeedback.user_id == user_id,
            )
            .values(comment=text[:2000], updated_at=func.now())
        )

    async def comments(self, site_id: int, work_date: date) -> list[str]:
        """Замечания к отчёту объекта за день — учитываются при следующей сборке."""
        rows = await self.session.scalars(
            select(ReportFeedback.comment)
            .where(
                ReportFeedback.site_id == site_id,
                ReportFeedback.work_date == work_date,
                ReportFeedback.comment.is_not(None),
            )
            .order_by(ReportFeedback.updated_at)
        )
        return [c for c in rows if c.strip()]


@dataclass(slots=True)
class CompanyStats:
    date_from: date
    date_to: date
    kinds: dict[str, int]
    voices: int
    transcribed: int
    corrected: int
    unprocessed: int
    failed: int
    reports_done: int
    reports_failed: int
    report_avg_seconds: float | None
    likes: int
    dislikes: int
    comments: list[str]
    activity: list[tuple[str, int]]  # прораб — сколько дней присылал сообщения


class StatsRepo:
    """Метрики пилота: насколько хорошо работает расшифровка и полезны ли отчёты."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def collect(self, company_id: int, date_from: date, date_to: date) -> CompanyStats:
        in_range = and_(
            Entry.company_id == company_id,
            Entry.work_date >= date_from,
            Entry.work_date <= date_to,
        )
        kinds = dict(
            (
                await self.session.execute(
                    select(Entry.kind, func.count()).where(in_range).group_by(Entry.kind)
                )
            ).all()
        )
        audio = and_(in_range, Entry.kind.in_(list(AUDIO_KINDS)))
        voices, transcribed, corrected = (
            await self.session.execute(
                select(
                    func.count(),
                    func.count().filter(
                        and_(Entry.transcript.is_not(None), Entry.transcript != "")
                    ),
                    func.count().filter(Entry.transcript_original.is_not(None)),
                ).where(audio)
            )
        ).one()
        unprocessed, failed = (
            await self.session.execute(
                select(
                    func.count().filter(
                        Entry.status.in_([EntryStatus.PENDING, EntryStatus.PROCESSING])
                    ),
                    func.count().filter(Entry.status == EntryStatus.FAILED),
                ).where(Entry.company_id == company_id)
            )
        ).one()

        company_sites = select(Site.id).where(Site.company_id == company_id)
        jobs_in_range = and_(
            ReportJob.site_id.in_(company_sites),
            ReportJob.work_date >= date_from,
            ReportJob.work_date <= date_to,
        )
        reports_done, reports_failed, avg_seconds = (
            await self.session.execute(
                select(
                    func.count().filter(ReportJob.status == EntryStatus.DONE),
                    func.count().filter(ReportJob.status == EntryStatus.FAILED),
                    func.avg(
                        func.extract("epoch", ReportJob.finished_at - ReportJob.created_at)
                    ).filter(ReportJob.status == EntryStatus.DONE),
                ).where(jobs_in_range)
            )
        ).one()

        fb_in_range = and_(
            ReportFeedback.site_id.in_(company_sites),
            ReportFeedback.work_date >= date_from,
            ReportFeedback.work_date <= date_to,
        )
        likes, dislikes = (
            await self.session.execute(
                select(
                    func.count().filter(ReportFeedback.rating > 0),
                    func.count().filter(ReportFeedback.rating < 0),
                ).where(fb_in_range)
            )
        ).one()
        comments = list(
            await self.session.scalars(
                select(ReportFeedback.comment)
                .where(fb_in_range, ReportFeedback.comment.is_not(None))
                .order_by(ReportFeedback.updated_at.desc())
                .limit(3)
            )
        )

        days = func.count(func.distinct(Entry.work_date))
        activity = [
            (name, n)
            for name, n in (
                await self.session.execute(
                    select(User.full_name, days)
                    .outerjoin(Entry, and_(Entry.user_id == User.id, in_range))
                    .where(User.company_id == company_id, User.role == UserRole.FOREMAN)
                    .group_by(User.id, User.full_name)
                    .order_by(days.desc(), User.full_name)
                )
            ).all()
        ]
        return CompanyStats(
            date_from=date_from,
            date_to=date_to,
            kinds=kinds,
            voices=voices,
            transcribed=transcribed,
            corrected=corrected,
            unprocessed=unprocessed,
            failed=failed,
            reports_done=reports_done,
            reports_failed=reports_failed,
            report_avg_seconds=float(avg_seconds) if avg_seconds is not None else None,
            likes=likes,
            dislikes=dislikes,
            comments=comments,
            activity=activity,
        )
