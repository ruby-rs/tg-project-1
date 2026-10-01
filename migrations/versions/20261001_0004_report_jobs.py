"""Очередь запросов отчёта

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 15:48:07.492066
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('report_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('work_date', sa.Date(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_report_jobs_site_id_sites'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_report_jobs_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_jobs'))
    )
    op.create_index('ix_report_jobs_queue', 'report_jobs', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ux_report_jobs_active', 'report_jobs', ['site_id', 'work_date', 'chat_id'], unique=True, postgresql_where=sa.text("status IN ('pending', 'processing')"))


def downgrade() -> None:
    op.drop_index('ux_report_jobs_active', table_name='report_jobs', postgresql_where=sa.text("status IN ('pending', 'processing')"))
    op.drop_index('ix_report_jobs_queue', table_name='report_jobs')
    op.drop_table('report_jobs')
