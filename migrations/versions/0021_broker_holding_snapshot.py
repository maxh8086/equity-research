"""broker_holding_snapshot and broker_holding_quarantine, append-only

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON = postgresql.ENUM(
    "missing_isin",
    "invalid_isin",
    "negative_quantity",
    "unparseable_number",
    "unknown_exchange",
    "duplicate_row",
    name="broker_holding_quarantine_reason",
    create_type=False,
)

SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"


def _append_only(table: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {table}_append_only
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_truncate
        BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def upgrade() -> None:
    REASON.create(op.get_bind())

    op.create_table(
        "broker_holding_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_label", sa.Text(), nullable=False),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("average_price", sa.Numeric(28, 6), nullable=False),
        sa.Column("last_price", sa.Numeric(28, 6), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_broker_holding_snapshot_isin_format"
        ),
        sa.CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_broker_holding_snapshot_exchange"),
        sa.CheckConstraint("quantity >= 0", name="ck_broker_holding_snapshot_quantity"),
        sa.CheckConstraint("average_price >= 0", name="ck_broker_holding_snapshot_average_price"),
        sa.CheckConstraint("last_price >= 0", name="ck_broker_holding_snapshot_last_price"),
        sa.CheckConstraint("account_label <> ''", name="ck_broker_holding_snapshot_account"),
        sa.CheckConstraint(
            "as_of >= snapshot_at", name="ck_broker_holding_snapshot_known_after_taken"
        ),
        sa.CheckConstraint(SHA256, name="ck_broker_holding_snapshot_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_broker_holding_snapshot_no_model"),
        sa.UniqueConstraint(
            "account_label",
            "isin",
            "exchange",
            "snapshot_at",
            "content_hash",
            name="uq_broker_holding_snapshot_key",
        ),
    )
    op.create_index(
        "ix_broker_holding_snapshot_pit",
        "broker_holding_snapshot",
        ["account_label", "isin", "snapshot_at"],
    )
    _append_only("broker_holding_snapshot")

    op.create_table(
        "broker_holding_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_label", sa.Text(), nullable=False),
        sa.Column("isin", sa.Text(), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("reason", REASON, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("row_number >= 1", name="ck_broker_holding_quarantine_row_number"),
        sa.CheckConstraint(
            "as_of >= snapshot_at", name="ck_broker_holding_quarantine_known_after_taken"
        ),
        sa.CheckConstraint(SHA256, name="ck_broker_holding_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_broker_holding_quarantine_no_model"),
        sa.UniqueConstraint("content_hash", "row_number", name="uq_broker_holding_quarantine_row"),
    )
    op.create_index("ix_broker_holding_quarantine_pit", "broker_holding_quarantine", ["as_of"])
    _append_only("broker_holding_quarantine")


def downgrade() -> None:
    op.drop_table("broker_holding_quarantine")
    op.drop_table("broker_holding_snapshot")
    REASON.drop(op.get_bind())
