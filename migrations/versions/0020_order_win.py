"""order_win and order_win_quarantine, append-only

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON = postgresql.ENUM(
    "pre_award",
    "excluded",
    "ambiguous",
    "no_value",
    "multiple_values",
    "invalid_isin",
    name="order_win_quarantine_reason",
    create_type=False,
)

SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"
KEY_SHA256 = "announcement_key ~ '^[0-9a-f]{64}$'"


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
        "order_win",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("announcement_key", sa.CHAR(64), nullable=False),
        sa.Column("announced_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_value_inr", sa.Numeric(28, 6), nullable=False),
        sa.Column("value_quote", sa.Text(), nullable=False),
        sa.Column("counterparty", sa.Text(), nullable=True),
        sa.Column("counterparty_quote", sa.Text(), nullable=True),
        sa.Column("execution_months", sa.Numeric(12, 4), nullable=True),
        sa.Column("execution_end", sa.Date(), nullable=True),
        sa.Column("period_quote", sa.Text(), nullable=True),
        sa.Column("matched_phrase", sa.Text(), nullable=False),
        sa.Column("missing", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_order_win_isin_format"),
        sa.CheckConstraint(KEY_SHA256, name="ck_order_win_announcement_key_sha256"),
        sa.CheckConstraint(SHA256, name="ck_order_win_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_order_win_no_model"),
        sa.CheckConstraint("order_value_inr > 0", name="ck_order_win_value_positive"),
        sa.CheckConstraint(
            "(counterparty IS NULL) = (counterparty_quote IS NULL)",
            name="ck_order_win_counterparty_with_quote",
        ),
        sa.CheckConstraint(
            "(execution_months IS NULL) = (period_quote IS NULL)",
            name="ck_order_win_period_with_quote",
        ),
        sa.CheckConstraint(
            "execution_end IS NULL OR execution_months IS NOT NULL",
            name="ck_order_win_end_needs_period",
        ),
        sa.CheckConstraint(
            "(counterparty IS NULL) = ('counterparty' = ANY(missing))"
            " AND (execution_months IS NULL) = ('execution_period' = ANY(missing))",
            name="ck_order_win_missing_names_the_gaps",
        ),
        sa.UniqueConstraint("announcement_key", "rule_version", name="uq_order_win_announcement_rule"),
    )
    op.create_index("ix_order_win_isin_announced", "order_win", ["isin", "announced_on"])
    _append_only("order_win")

    op.create_table(
        "order_win_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("announcement_key", sa.CHAR(64), nullable=False),
        sa.Column("announced_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", REASON, nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "isin IS NULL OR isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_order_win_quarantine_isin_format"
        ),
        sa.CheckConstraint(KEY_SHA256, name="ck_order_win_quarantine_announcement_key_sha256"),
        sa.CheckConstraint(SHA256, name="ck_order_win_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_order_win_quarantine_no_model"),
        sa.UniqueConstraint(
            "announcement_key", "rule_version", name="uq_order_win_quarantine_announcement_rule"
        ),
    )
    op.create_index("ix_order_win_quarantine_pit", "order_win_quarantine", ["as_of"])
    _append_only("order_win_quarantine")


def downgrade() -> None:
    op.drop_table("order_win_quarantine")
    op.drop_table("order_win")
    REASON.drop(op.get_bind())
