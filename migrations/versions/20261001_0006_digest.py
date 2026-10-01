"""Вечерняя сводка и напоминания: настройки компании и плановые рассылки

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 16:33:11.013672
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('scheduled_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('company_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('work_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_scheduled_runs_company_id_companies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_runs')),
    sa.UniqueConstraint('company_id', 'kind', 'work_date', name='uq_scheduled_runs_day')
    )
    op.create_index('ix_scheduled_runs_queue', 'scheduled_runs', ['status', 'next_attempt_at'], unique=False)
    op.add_column('companies', sa.Column('digest_time', sa.Time(), server_default=sa.text("'19:00'"), nullable=True))
    op.add_column('companies', sa.Column('reminder_time', sa.Time(), server_default=sa.text("'17:00'"), nullable=True))
    op.add_column('companies', sa.Column('work_days', sa.String(length=7), server_default='123456', nullable=False))


def downgrade() -> None:
    op.drop_column('companies', 'work_days')
    op.drop_column('companies', 'reminder_time')
    op.drop_column('companies', 'digest_time')
    op.drop_index('ix_scheduled_runs_queue', table_name='scheduled_runs')
    op.drop_table('scheduled_runs')
