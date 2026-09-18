"""Store sourced contacts and outreach suppressions.

Revision ID: d6f1e84a0912
Revises: c3e8a19d4b62
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6f1e84a0912"
down_revision: Union[str, Sequence[str], None] = "c3e8a19d4b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("identity_key", sa.String(500), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("email", sa.String(255)),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("source_title", sa.String(500), nullable=False),
        sa.Column("source_type", sa.String(60), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "identity_key", name="uq_contact_identity"),
    )
    op.create_index("ix_contacts_account_id", "contacts", ["account_id"])
    op.create_table(
        "suppressions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "value", name="uq_suppression_kind_value"),
    )


def downgrade() -> None:
    op.drop_table("suppressions")
    op.drop_index("ix_contacts_account_id", table_name="contacts")
    op.drop_table("contacts")
