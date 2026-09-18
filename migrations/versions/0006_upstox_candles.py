"""upstox_candle and upstox_candle_quarantine, append-only

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUARANTINE_REASON = postgresql.ENUM(
    "shape_changed",
    "unknown_instrument",
    "malformed_candle",
    "ohlc_inconsistent",
    "duplicate_date",
    name="upstox_quarantine_reason",
    create_type=False,
)

TABLES = ("upstox_candle", "upstox_candle_quarantine")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    QUARANTINE_REASON.create(bind)

    op.create_table(
        "upstox_candle",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("instrument_key", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(20, 4), nullable=False),
        sa.Column("high", sa.Numeric(20, 4), nullable=False),
        sa.Column("low", sa.Numeric(20, 4), nullable=False),
        sa.Column("close", sa.Numeric(20, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("open_interest", sa.BigInteger(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_upstox_candle_isin_format"),
        sa.CheckConstraint(
            "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0",
            name="ck_upstox_candle_prices_non_negative",
        ),
        sa.CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ck_upstox_candle_ohlc_consistent",
        ),
        sa.CheckConstraint("volume >= 0 AND open_interest >= 0", name="ck_upstox_candle_counts_non_negative"),
        sa.CheckConstraint(
            "(as_of AT TIME ZONE 'Asia/Kolkata')::date >= trade_date", name="ck_upstox_candle_as_of_after_trade"
        ),
        sa.CheckConstraint(SHA256, name="ck_upstox_candle_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_upstox_candle_no_model"),
        sa.UniqueConstraint("isin", "trade_date", "as_of", "rule_version", name="uq_upstox_candle_publication"),
    )
    op.create_index("ix_upstox_candle_isin_pit", "upstox_candle", ["isin", "trade_date"])

    op.create_table(
        "upstox_candle_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("candle_index", sa.Integer(), nullable=True),
        sa.Column("raw_candle", sa.Text(), nullable=True),
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
            "(candle_index IS NULL) = (raw_candle IS NULL)",
            name="ck_upstox_candle_quarantine_candle_fields_together",
        ),
        sa.CheckConstraint(SHA256, name="ck_upstox_candle_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_upstox_candle_quarantine_no_model"),
    )
    op.create_index("ix_upstox_candle_quarantine_pit", "upstox_candle_quarantine", ["as_of"])

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
