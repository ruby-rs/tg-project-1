import secrets
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


DEFAULT_WORK_DAYS = "123456"


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        date: Date,
        time: Time,
        dict[str, Any]: JSONB,
    }


class UserRole(StrEnum):
    OWNER = "owner"  # руководитель, создал компанию
    MANAGER = "manager"  # получает сводки, видит все объекты
    FOREMAN = "foreman"  # прораб, шлёт отчёты с объекта


class EntryKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO_NOTE = "video_note"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"


AUDIO_KINDS = frozenset({EntryKind.VOICE, EntryKind.AUDIO, EntryKind.VIDEO_NOTE})


class EntryStatus(StrEnum):
    PENDING = "pending"  # ждёт воркера (скачать файл, расшифровать, описать фото)
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class Company(Base):
    """Арендатор: к компании в дальнейшем привязывается подписка."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    invite_code: Mapped[str] = mapped_column(String(32), unique=True)
    # Вечерняя сводка руководителям и напоминание прорабам (местное время; None — выкл.)
    digest_time: Mapped[time | None] = mapped_column(
        default=time(19, 0), server_default=text("'19:00'")
    )
    reminder_time: Mapped[time | None] = mapped_column(
        default=time(17, 0), server_default=text("'17:00'")
    )
    # Рабочие дни — номера дней недели ISO (1 — пн … 7 — вс)
    work_days: Mapped[str] = mapped_column(
        String(7), default=DEFAULT_WORK_DAYS, server_default=DEFAULT_WORK_DAYS
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    sites: Mapped[list["Site"]] = relationship(back_populates="company")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.FOREMAN)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str | None] = mapped_column(String(64))
    # Объект, к которому сейчас привязываются входящие сообщения прораба
    current_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    # Согласие на обработку персональных данных (152-ФЗ); None — не дано или отозвано
    consent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    company: Mapped[Company | None] = relationship(lazy="joined")
    current_site: Mapped["Site | None"] = relationship(lazy="joined")

    @property
    def is_manager(self) -> bool:
        return self.role in (UserRole.OWNER, UserRole.MANAGER)


class Site(Base):
    """Строительный объект."""

    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_sites_company_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ссылка-приглашение сразу на объект: прораб попадает и в компанию, и на объект
    invite_code: Mapped[str] = mapped_column(String(32), unique=True, default=new_invite_code)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    company: Mapped[Company] = relationship(back_populates="sites")


class SiteMember(Base):
    """Прораб допущен к объекту. Руководители видят все объекты без записей здесь."""

    __tablename__ = "site_members"

    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Entry(Base):
    """Одно сообщение прораба: текст, голосовое, фото и т.д.

    Таблица одновременно служит очередью задач для воркера
    (status / attempts / next_attempt_at / locked_at).
    """

    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("tg_chat_id", "tg_message_id", name="uq_entries_tg_message"),
        Index("ix_entries_site_date", "site_id", "work_date"),
        Index("ix_entries_queue", "status", "next_attempt_at"),
        Index("ix_entries_transcript_message", "tg_chat_id", "transcript_message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    # Без каскада: архив сообщений нужен и после ухода прораба из компании
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=EntryStatus.PENDING)

    work_date: Mapped[date]
    sent_at: Mapped[datetime]  # время отправки по данным Telegram (UTC)
    taken_at: Mapped[datetime | None]  # время съёмки из EXIF, если фото прислали файлом

    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    media_group_id: Mapped[str | None] = mapped_column(String(64))

    tg_file_id: Mapped[str | None] = mapped_column(String(255))
    tg_file_unique_id: Mapped[str | None] = mapped_column(String(64))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    duration: Mapped[int | None] = mapped_column(Integer)
    # Путь относительно MEDIA_ROOT и хеш — для доказательной базы в спорах
    file_path: Mapped[str | None] = mapped_column(String(512))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    file_sha256: Mapped[str | None] = mapped_column(String(64))

    text: Mapped[str | None] = mapped_column(Text)  # текст сообщения или подпись к медиа
    edited_at: Mapped[datetime | None]  # прораб отредактировал текст или подпись
    transcript: Mapped[str | None] = mapped_column(Text)
    # Исходная расшифровка Whisper, если прораб её исправил
    transcript_original: Mapped[str | None] = mapped_column(Text)
    # Сообщение бота с расшифровкой: ответ на него исправляет расшифровку
    transcript_message_id: Mapped[int | None] = mapped_column(BigInteger)
    photo_description: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    locked_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    company: Mapped[Company] = relationship()
    site: Mapped[Site | None] = relationship()
    user: Mapped[User] = relationship()


class ReportJob(Base):
    """Запрос отчёта: бот ставит задачу, воркер собирает отчёт и присылает его в чат."""

    __tablename__ = "report_jobs"
    __table_args__ = (
        Index("ix_report_jobs_queue", "status", "next_attempt_at"),
        # Повторный /report, пока прошлый не готов, не создаёт дубль
        Index(
            "ux_report_jobs_active",
            "site_id",
            "work_date",
            "chat_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    work_date: Mapped[date]
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default=EntryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    locked_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]

    site: Mapped[Site] = relationship()


class ScheduledKind(StrEnum):
    DIGEST = "digest"  # вечерняя сводка руководителям
    REMINDER = "reminder"  # напоминание прорабам без сообщений за день


class ScheduledRun(Base):
    """Плановая рассылка компании за рабочий день. Уникальность защищает от повторов."""

    __tablename__ = "scheduled_runs"
    __table_args__ = (
        UniqueConstraint("company_id", "kind", "work_date", name="uq_scheduled_runs_day"),
        Index("ix_scheduled_runs_queue", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    work_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(16), default=EntryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    locked_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]

    company: Mapped[Company] = relationship()


class DailyReport(Base):
    """Сформированный LLM отчёт по объекту за день (последняя версия)."""

    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("site_id", "work_date", name="uq_daily_reports_site_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    work_date: Mapped[date]
    data: Mapped[dict[str, Any]]
    model: Mapped[str] = mapped_column(String(128))
    entries_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    site: Mapped[Site] = relationship()
