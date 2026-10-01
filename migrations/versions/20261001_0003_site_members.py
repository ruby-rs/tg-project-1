"""Доступ прорабов к объектам и приглашения на объект

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 15:44:56.116514
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_members",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_site_members_site_id_sites"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_site_members_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("site_id", "user_id", name=op.f("pk_site_members")),
    )

    # Коды приглашений для уже существующих объектов
    op.add_column("sites", sa.Column("invite_code", sa.String(length=32), nullable=True))
    op.execute("UPDATE sites SET invite_code = substr(md5(random()::text || id::text), 1, 16)")
    op.alter_column("sites", "invite_code", nullable=False)
    op.create_unique_constraint(op.f("uq_sites_invite_code"), "sites", ["invite_code"])

    # Сохраняем доступ, который у прорабов был до появления ограничений:
    # объекты, куда они уже присылали сообщения, и текущий выбранный объект
    op.execute(
        """
        INSERT INTO site_members (site_id, user_id)
        SELECT DISTINCT site_id, user_id FROM entries WHERE site_id IS NOT NULL
        UNION
        SELECT current_site_id, id FROM users WHERE current_site_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_sites_invite_code"), "sites", type_="unique")
    op.drop_column("sites", "invite_code")
    op.drop_table("site_members")
