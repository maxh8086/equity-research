"""company_event table and enums, append-only

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPANY_EVENT_SEVERITY = postgresql.ENUM(
    "critical",
    "high",
    "medium",
    "low",
    name="company_event_severity",
    create_type=False,
)

COMPANY_EVENT_TYPE = postgresql.ENUM(
    "cwip_to_gross_block",
    "revenue_step_up",
    "margin_compression",
    "debt_spike",
    "promoter_pledge",
    "promoter_pledge_invoked",
    "rating_downgrade",
    "rating_withdrawal",
    "raid_regulatory",
    name="company_event_type",
    create_type=False,
)

TABLE = "company_event"

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"


def upgrade() -> None:
    bind = op.get_bind()
    COMPANY_EVENT_SEVERITY.create(bind)
    COMPANY_EVENT_TYPE.create(bind)

    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("event_type", COMPANY_EVENT_TYPE, nullable=False),
        sa.Column("severity", COMPANY_EVENT_SEVERITY, nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=True),
        sa.Column("value", sa.Numeric(28, 6), nullable=True),
        sa.Column("threshold", sa.Numeric(28, 6), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_company_event_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_company_event_no_model"),
        sa.CheckConstraint(
            "evidence_url IS NOT NULL AND evidence_url != ''",
            name="ck_company_event_evidence_url_required",
        ),
        sa.CheckConstraint(SHA256, name="ck_company_event_content_hash_sha256"),
    )
    op.create_index("ix_company_event_isin_date", TABLE, ["isin", "event_date"])
    op.create_index("ix_company_event_type_date", TABLE, ["event_type", "event_date"])

    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_append_only
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_truncate
        BEFORE TRUNCATE ON {TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table(TABLE)
    bind = op.get_bind()
    COMPANY_EVENT_TYPE.drop(bind)
    COMPANY_EVENT_SEVERITY.drop(bind)
