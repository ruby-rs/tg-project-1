"""Начальная схема: компании, пользователи, объекты, сообщения, отчёты

Revision ID: 0001
Revises: 
Create Date: 2026-09-30 21:35:29.802423
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('companies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('invite_code', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_companies')),
    sa.UniqueConstraint('invite_code', name=op.f('uq_companies_invite_code'))
    )
    op.create_table('sites',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('address', sa.String(length=500), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_sites_company_id_companies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sites')),
    sa.UniqueConstraint('company_id', 'name', name='uq_sites_company_name')
    )
    op.create_table('daily_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('work_date', sa.Date(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('entries_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_daily_reports_site_id_sites'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_reports')),
    sa.UniqueConstraint('site_id', 'work_date', name='uq_daily_reports_site_date')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_id', sa.BigInteger(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('current_site_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_users_company_id_companies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['current_site_id'], ['sites.id'], name=op.f('fk_users_current_site_id_sites'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('tg_id', name=op.f('uq_users_tg_id'))
    )
    op.create_table('entries',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('work_date', sa.Date(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('taken_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tg_chat_id', sa.BigInteger(), nullable=False),
    sa.Column('tg_message_id', sa.BigInteger(), nullable=False),
    sa.Column('media_group_id', sa.String(length=64), nullable=True),
    sa.Column('tg_file_id', sa.String(length=255), nullable=True),
    sa.Column('tg_file_unique_id', sa.String(length=64), nullable=True),
    sa.Column('file_name', sa.String(length=255), nullable=True),
    sa.Column('mime_type', sa.String(length=128), nullable=True),
    sa.Column('duration', sa.Integer(), nullable=True),
    sa.Column('file_path', sa.String(length=512), nullable=True),
    sa.Column('file_size', sa.BigInteger(), nullable=True),
    sa.Column('file_sha256', sa.String(length=64), nullable=True),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('photo_description', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_entries_company_id_companies'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_entries_site_id_sites'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_entries_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_entries')),
    sa.UniqueConstraint('tg_chat_id', 'tg_message_id', name='uq_entries_tg_message')
    )
    op.create_index('ix_entries_queue', 'entries', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_entries_site_date', 'entries', ['site_id', 'work_date'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_entries_site_date', table_name='entries')
    op.drop_index('ix_entries_queue', table_name='entries')
    op.drop_table('entries')
    op.drop_table('users')
    op.drop_table('daily_reports')
    op.drop_table('sites')
    op.drop_table('companies')
