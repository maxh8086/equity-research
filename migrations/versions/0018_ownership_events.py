"""insider_trade, stake_disclosure, bulk_block_deal, scheduled_event, index_event tables

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACQUISITION_MODE = postgresql.ENUM(
    "open_market",
    "preferential_allotment",
    "esos_esop",
    "off_market",
    "gift",
    "inheritance",
    "pledge_invocation",
    "other",
    name="acquisition_mode",
    create_type=False,
)

DISCLOSURE_TYPE = postgresql.ENUM(
    "sast_5pct",
    "sast_10pct",
    "sast_25pct",
    "sast_creeping",
    "pledge_created",
    "pledge_released",
    "pledge_invoked",
    "reclassification",
    name="disclosure_type",
    create_type=False,
)

DEAL_TYPE = postgresql.ENUM(
    "bulk",
    "block",
    name="deal_type",
    create_type=False,
)

DEAL_SIDE = postgresql.ENUM(
    "inflow",
    "outflow",
    name="deal_side",
    create_type=False,
)

SCHEDULED_EVENT_TYPE = postgresql.ENUM(
    "board_meeting",
    "results",
    "agm",
    "egm",
    "record_date",
    "ex_dividend",
    "rights_record",
    "buyback_open",
    "buyback_close",
    "ofs_open",
    "ofs_close",
    name="scheduled_event_type",
    create_type=False,
)

SCHEDULED_EVENT_SEVERITY = postgresql.ENUM(
    "critical",
    "high",
    "medium",
    "low",
    name="scheduled_event_severity",
    create_type=False,
)

INDEX_EVENT_TYPE = postgresql.ENUM(
    "inclusion",
    "exclusion",
    "weight_change",
    "fo_inclusion",
    "fo_exclusion",
    name="index_event_type",
    create_type=False,
)

INDEX_EVENT_STATUS = postgresql.ENUM(
    "announced",
    "effective",
    "revised",
    "cancelled",
    name="index_event_status",
    create_type=False,
)

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

_PROVENANCE = [
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
]


def _append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only "
        f"BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate "
        f"BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
    )


def upgrade() -> None:
    bind = op.get_bind()

    # Extend index_code enum with new values (safe on existing data)
    for val in ("msci_em", "msci_india", "ftse_all_world", "bse_sensex"):
        op.execute(f"ALTER TYPE index_code ADD VALUE IF NOT EXISTS '{val}'")

    # Create new enum types
    ACQUISITION_MODE.create(bind)
    DISCLOSURE_TYPE.create(bind)
    DEAL_TYPE.create(bind)
    DEAL_SIDE.create(bind)
    SCHEDULED_EVENT_TYPE.create(bind)
    SCHEDULED_EVENT_SEVERITY.create(bind)
    INDEX_EVENT_TYPE.create(bind)
    INDEX_EVENT_STATUS.create(bind)

    # insider_trade
    op.create_table(
        "insider_trade",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("person_name", sa.Text(), nullable=False),
        sa.Column("person_category", sa.Text(), nullable=False),
        sa.Column("acquisition_mode", ACQUISITION_MODE, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 0), nullable=False),
        sa.Column("price_per_share", sa.Numeric(28, 6), nullable=False),
        sa.Column("post_trade_holding_pct", sa.Numeric(10, 6), nullable=True),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_insider_trade_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_insider_trade_no_model"),
        sa.CheckConstraint(SHA256, name="ck_insider_trade_content_hash_sha256"),
    )
    op.create_index("ix_insider_trade_isin_date", "insider_trade", ["isin", "trade_date"])
    _append_only_triggers("insider_trade")

    # stake_disclosure
    op.create_table(
        "stake_disclosure",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("acquirer_name", sa.Text(), nullable=False),
        sa.Column("disclosure_type", DISCLOSURE_TYPE, nullable=False),
        sa.Column("disclosure_date", sa.Date(), nullable=False),
        sa.Column("shares_acquired", sa.Numeric(28, 0), nullable=True),
        sa.Column("post_acquisition_pct", sa.Numeric(10, 6), nullable=True),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_stake_disclosure_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_stake_disclosure_no_model"),
        sa.CheckConstraint(SHA256, name="ck_stake_disclosure_content_hash_sha256"),
    )
    op.create_index(
        "ix_stake_disclosure_isin_date", "stake_disclosure", ["isin", "disclosure_date"]
    )
    _append_only_triggers("stake_disclosure")

    # bulk_block_deal
    op.create_table(
        "bulk_block_deal",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("deal_type", DEAL_TYPE, nullable=False),
        sa.Column("deal_date", sa.Date(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 0), nullable=False),
        sa.Column("price_per_share", sa.Numeric(28, 6), nullable=False),
        sa.Column("deal_side", DEAL_SIDE, nullable=False),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_bulk_block_deal_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_bulk_block_deal_no_model"),
        sa.CheckConstraint(SHA256, name="ck_bulk_block_deal_content_hash_sha256"),
    )
    op.create_index("ix_bulk_block_deal_isin_date", "bulk_block_deal", ["isin", "deal_date"])
    _append_only_triggers("bulk_block_deal")

    # scheduled_event
    op.create_table(
        "scheduled_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("event_type", SCHEDULED_EVENT_TYPE, nullable=False),
        sa.Column("severity", SCHEDULED_EVENT_SEVERITY, nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_scheduled_event_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_scheduled_event_no_model"),
        sa.CheckConstraint(SHA256, name="ck_scheduled_event_content_hash_sha256"),
    )
    op.create_index("ix_scheduled_event_isin_date", "scheduled_event", ["isin", "event_date"])
    op.create_index(
        "ix_scheduled_event_type_date", "scheduled_event", ["event_type", "event_date"]
    )
    _append_only_triggers("scheduled_event")

    # index_event
    op.create_table(
        "index_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column(
            "index_code",
            postgresql.ENUM(name="index_code", create_type=False),
            nullable=False,
        ),
        sa.Column("event_type", INDEX_EVENT_TYPE, nullable=False),
        sa.Column("status", INDEX_EVENT_STATUS, nullable=False),
        sa.Column("announced_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("old_weight", sa.Numeric(10, 6), nullable=True),
        sa.Column("new_weight", sa.Numeric(10, 6), nullable=True),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_index_event_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_index_event_no_model"),
        sa.CheckConstraint(SHA256, name="ck_index_event_content_hash_sha256"),
    )
    op.create_index("ix_index_event_isin", "index_event", ["isin"])
    op.create_index("ix_index_event_code_status", "index_event", ["index_code", "status"])
    _append_only_triggers("index_event")


def downgrade() -> None:
    op.drop_table("index_event")
    op.drop_table("scheduled_event")
    op.drop_table("bulk_block_deal")
    op.drop_table("stake_disclosure")
    op.drop_table("insider_trade")

    bind = op.get_bind()
    INDEX_EVENT_STATUS.drop(bind)
    INDEX_EVENT_TYPE.drop(bind)
    SCHEDULED_EVENT_SEVERITY.drop(bind)
    SCHEDULED_EVENT_TYPE.drop(bind)
    DEAL_SIDE.drop(bind)
    DEAL_TYPE.drop(bind)
    DISCLOSURE_TYPE.drop(bind)
    ACQUISITION_MODE.drop(bind)
    # Note: index_code enum values cannot be removed in PostgreSQL without
    # recreating the type. The four new values remain after downgrade.
