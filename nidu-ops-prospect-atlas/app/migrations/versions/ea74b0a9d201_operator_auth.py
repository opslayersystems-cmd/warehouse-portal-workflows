"""Add local operator accounts.

Revision ID: ea74b0a9d201
Revises: 92bd88dbcb33
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ea74b0a9d201"
down_revision: Union[str, Sequence[str], None] = "92bd88dbcb33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operators",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operators_username", "operators", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_operators_username", table_name="operators")
    op.drop_table("operators")
