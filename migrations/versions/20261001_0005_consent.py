"""Согласие на обработку персональных данных

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01 15:50:38.786934
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('consent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'consent_at')
