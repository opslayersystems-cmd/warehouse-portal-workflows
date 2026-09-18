"""Persist bounded research jobs and provider rate slots.

Revision ID: c3e8a19d4b62
Revises: b7c44c63f102
"""

from typing import Sequence, Union
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a19d4b62"
down_revision: Union[str, Sequence[str], None] = "b7c44c63f102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("active_account_id", sa.String(36), nullable=True, unique=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_jobs_account_id", "research_jobs", ["account_id"])
    op.create_index("ix_research_jobs_ready", "research_jobs", ["status", "available_at"])
    op.create_table(
        "provider_throttles",
        sa.Column("provider", sa.String(20), primary_key=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "provider_throttles",
            sa.column("provider", sa.String(20)),
            sa.column("next_allowed_at", sa.DateTime(timezone=True)),
        ),
        [{"provider": "openai", "next_allowed_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_table("provider_throttles")
    op.drop_index("ix_research_jobs_ready", table_name="research_jobs")
    op.drop_index("ix_research_jobs_account_id", table_name="research_jobs")
    op.drop_table("research_jobs")
