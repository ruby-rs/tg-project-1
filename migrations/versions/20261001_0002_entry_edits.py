"""Правки сообщений и исправление расшифровок

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 15:42:33.188489
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('entries', sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('entries', sa.Column('transcript_original', sa.Text(), nullable=True))
    op.add_column('entries', sa.Column('transcript_message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_entries_transcript_message', 'entries', ['tg_chat_id', 'transcript_message_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_entries_transcript_message', table_name='entries')
    op.drop_column('entries', 'transcript_message_id')
    op.drop_column('entries', 'transcript_original')
    op.drop_column('entries', 'edited_at')
