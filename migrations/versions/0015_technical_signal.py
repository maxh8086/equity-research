"""technical_signal and watchlist_entry tables, append-only

Revision ID: 0015
Revises: 0011
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TECHNICAL_SIGNAL_TYPE = postgresql.ENUM(
    "volume_spike_up",
    "volume_spike_down",
    "consolidation_breakout",
    "consolidation_breakdown",
    "ath_breakout",
    name="technical_signal_type",
    create_type=False,
)

WATCHLIST_ENTRY_STATUS = postgresql.ENUM(
    "open",
    "promoted",
    "expired",
    "invalidated",
    name="watchlist_entry_status",
    create_type=False,
)

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"


def upgrade() -> None:
    bind = op.get_bind()
    TECHNICAL_SIGNAL_TYPE.create(bind)
    WATCHLIST_ENTRY_STATUS.create(bind)

    op.create_table(
        "technical_signal",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("signal_type", TECHNICAL_SIGNAL_TYPE, nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("close_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("volume_median_50d", sa.Numeric(20, 4), nullable=True),
        sa.Column("price_return_1d", sa.Numeric(10, 6), nullable=True),
        sa.Column("consolidation_start", sa.Date(), nullable=True),
        sa.Column("consolidation_high", sa.Numeric(20, 4), nullable=True),
        sa.Column("consolidation_low", sa.Numeric(20, 4), nullable=True),
        sa.Column("ath_since", sa.Date(), nullable=True),
        sa.Column("adjustment_factor", sa.Numeric(20, 10), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        # Provenance columns
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_technical_signal_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_technical_signal_no_model"),
        sa.CheckConstraint("close_price > 0", name="ck_technical_signal_close_price_positive"),
        sa.CheckConstraint(SHA256, name="ck_technical_signal_content_hash_sha256"),
        sa.UniqueConstraint(
            "isin", "signal_type", "signal_date", "rule_version",
            name="uq_technical_signal_version",
        ),
    )
    op.create_index("ix_technical_signal_isin_date", "technical_signal", ["isin", "signal_date"])

    op.execute(
        """
        CREATE TRIGGER technical_signal_append_only
        BEFORE UPDATE OR DELETE ON technical_signal
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER technical_signal_no_truncate
        BEFORE TRUNCATE ON technical_signal
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )

    op.create_table(
        "watchlist_entry",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("status", WATCHLIST_ENTRY_STATUS, nullable=False),
        sa.Column("opened_by_signal_id", sa.BigInteger(),
                  sa.ForeignKey("technical_signal.id"), nullable=False),
        sa.Column("opened_date", sa.Date(), nullable=False),
        sa.Column("status_date", sa.Date(), nullable=False),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=False),
        # Provenance columns
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_watchlist_entry_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_watchlist_entry_no_model"),
        sa.CheckConstraint(SHA256, name="ck_watchlist_entry_content_hash_sha256"),
    )
    op.create_index("ix_watchlist_entry_isin_status", "watchlist_entry", ["isin", "status"])
    op.create_index("ix_watchlist_entry_status_date", "watchlist_entry", ["status", "status_date"])

    op.execute(
        """
        CREATE TRIGGER watchlist_entry_append_only
        BEFORE UPDATE OR DELETE ON watchlist_entry
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER watchlist_entry_no_truncate
        BEFORE TRUNCATE ON watchlist_entry
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("watchlist_entry")
    op.drop_table("technical_signal")
    bind = op.get_bind()
    WATCHLIST_ENTRY_STATUS.drop(bind)
    TECHNICAL_SIGNAL_TYPE.drop(bind)
