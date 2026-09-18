"""Bind verified Google subject to a local operator.

Revision ID: b7c44c63f102
Revises: ea74b0a9d201
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c44c63f102"
down_revision: Union[str, Sequence[str], None] = "ea74b0a9d201"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("operators", sa.Column("google_subject", sa.String(length=255), nullable=True))
    op.create_index("ix_operators_google_subject", "operators", ["google_subject"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_operators_google_subject", table_name="operators")
    op.drop_column("operators", "google_subject")
