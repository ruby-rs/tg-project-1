"""Повторная расшифровка голосового по кнопке

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'entries',
        sa.Column('retranscribe', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('entries', 'retranscribe')
