"""nse_bhavcopy_row and nse_bhavcopy_quarantine, append-only

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUARANTINE_REASON = postgresql.ENUM(
    "shape_changed",
    "row_count",
    "malformed_row",
    "invalid_isin",
    "duplicate_row",
    name="bhavcopy_quarantine_reason",
    create_type=False,
)

TABLES = ("nse_bhavcopy_row", "nse_bhavcopy_quarantine")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    QUARANTINE_REASON.create(bind)

    op.create_table(
        "nse_bhavcopy_row",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("open", sa.Numeric(20, 4), nullable=False),
        sa.Column("high", sa.Numeric(20, 4), nullable=False),
        sa.Column("low", sa.Numeric(20, 4), nullable=False),
        sa.Column("close", sa.Numeric(20, 4), nullable=False),
        sa.Column("prev_close", sa.Numeric(20, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("turnover", sa.Numeric(24, 4), nullable=False),
        sa.Column("trades", sa.BigInteger(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_nse_bhavcopy_row_isin_format"),
        sa.CheckConstraint(
            "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0 AND prev_close >= 0",
            name="ck_nse_bhavcopy_row_prices_non_negative",
        ),
        sa.CheckConstraint("high >= low", name="ck_nse_bhavcopy_row_high_not_below_low"),
        sa.CheckConstraint(
            "volume >= 0 AND turnover >= 0 AND trades >= 0", name="ck_nse_bhavcopy_row_counts_non_negative"
        ),
        sa.CheckConstraint(SHA256, name="ck_nse_bhavcopy_row_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_nse_bhavcopy_row_no_model"),
        sa.UniqueConstraint(
            "trade_date", "isin", "series", "rule_version", name="uq_nse_bhavcopy_row_publication"
        ),
    )
    op.create_index("ix_nse_bhavcopy_row_isin_pit", "nse_bhavcopy_row", ["isin", "trade_date"])
    op.create_index("ix_nse_bhavcopy_row_symbol_pit", "nse_bhavcopy_row", ["symbol", "trade_date"])

    op.create_table(
        "nse_bhavcopy_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("series", sa.Text(), nullable=True),
        sa.Column("raw_row", sa.Text(), nullable=True),
        sa.Column("reason", QUARANTINE_REASON, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(row_number IS NULL) = (raw_row IS NULL)",
            name="ck_nse_bhavcopy_quarantine_row_fields_together",
        ),
        sa.CheckConstraint(SHA256, name="ck_nse_bhavcopy_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_nse_bhavcopy_quarantine_no_model"),
    )
    op.create_index("ix_nse_bhavcopy_quarantine_pit", "nse_bhavcopy_quarantine", ["as_of"])

    for table in TABLES:
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


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    bind = op.get_bind()
    QUARANTINE_REASON.drop(bind)
