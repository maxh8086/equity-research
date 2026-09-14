"""financial_facts with as_of provenance, append-only

Revision ID: 0001
Revises:
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Shared by every append-only store. Row triggers cover UPDATE/DELETE;
    # the statement trigger covers TRUNCATE.
    op.execute(
        """
        CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only (R2): % rejected', TG_TABLE_NAME, TG_OP;
        END
        $$
        """
    )

    op.create_table(
        "financial_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column(
            "consolidation",
            sa.Enum("standalone", "consolidated", name="consolidation"),
            nullable=False,
        ),
        sa.Column(
            "fact_kind", sa.Enum("reported", "computed", name="fact_kind"), nullable=False
        ),
        sa.Column("line_item", sa.Text(), nullable=False),
        sa.Column("xbrl_element", sa.Text(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(28, 6), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_financial_facts_isin_format"
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_start <= period_end",
            name="ck_financial_facts_period_order",
        ),
        sa.CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end",
            name="ck_financial_facts_as_of_after_period_end",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_financial_facts_content_hash_sha256"
        ),
        sa.CheckConstraint(
            "(fact_kind = 'reported') = (xbrl_element IS NOT NULL)",
            name="ck_financial_facts_xbrl_element_iff_reported",
        ),
        sa.CheckConstraint("model_version IS NULL", name="ck_financial_facts_no_model"),
        sa.UniqueConstraint(
            "isin",
            "consolidation",
            "line_item",
            "period_start",
            "period_end",
            "as_of",
            name="uq_financial_facts_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_financial_facts_pit",
        "financial_facts",
        ["isin", "consolidation", "line_item", "period_end", "as_of"],
    )

    op.execute(
        """
        CREATE TRIGGER financial_facts_append_only
        BEFORE UPDATE OR DELETE ON financial_facts
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER financial_facts_no_truncate
        BEFORE TRUNCATE ON financial_facts
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("financial_facts")
    op.execute("DROP TYPE fact_kind")
    op.execute("DROP TYPE consolidation")
    op.execute("DROP FUNCTION forbid_mutation()")
