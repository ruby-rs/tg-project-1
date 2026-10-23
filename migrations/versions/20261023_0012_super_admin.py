"""Главный администратор: отключение компаний и остановка всего бота

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'service_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paused_by', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_service_state')),
    )
    op.execute("INSERT INTO service_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table('service_state')
    op.drop_column('companies', 'disabled_at')
