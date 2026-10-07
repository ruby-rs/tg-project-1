"""Пересборка отчёта по запросу

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'report_jobs',
        sa.Column('rebuild', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('report_jobs', 'rebuild')
