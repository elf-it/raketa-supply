"""Отозванные сессии: выход закрывает вход (задача G-20).

До этой ревизии выход удалял куку в браузере, а подписанный токен оставался
действительным до конца своих двенадцати часов: на общем компьютере «я вышел»
означало «кука удалена здесь». Таблица хранит номера отозванных токенов —
список отозванных короче списка действующих, и строка в нём нужна лишь
до ``expires_at`` самого токена, после чего его отвергает подпись.

Индекс по ``expires_at`` нужен чистке (`app.cleanup`): она удаляет строки,
чей токен и так просрочен.

Revision ID: 193aa5a69cc6
Revises: 3b5734954f4c
Create Date: 2026-11-18 16:53:25.679305
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "193aa5a69cc6"
down_revision: str | None = "3b5734954f4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_sessions",
        sa.Column("jti", sa.String(length=43), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"])


def downgrade() -> None:
    # Откат возвращает схему, а не состояние: отозванные сессии после него
    # снова станут действующими до конца своего срока.
    op.drop_index("ix_revoked_sessions_expires_at", table_name="revoked_sessions")
    op.drop_table("revoked_sessions")
