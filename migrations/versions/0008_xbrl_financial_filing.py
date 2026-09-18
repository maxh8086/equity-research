"""financial_facts gains rule_version; financial_filing and financial_facts_quarantine, append-only

The XBRL results parser (ingest/nse_xbrl) writes one `financial_filing` row per
parsed file and one `financial_facts` row per mapped fact. A corrected mapping
reparses stored bytes and adds rows under a new rule_version, dated at the
file's original as_of (docs/temporal-model.md), so rule_version joins the
uniqueness key. Rows written before this migration carry 'pre-0008': the
column default is set in DDL, which fires no row trigger, then dropped.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSOLIDATION = postgresql.ENUM("standalone", "consolidated", name="consolidation", create_type=False)
TAXONOMY = postgresql.ENUM(
    "ind_as", "nbfc", "bank", "life_insurance", "general_insurance", name="xbrl_taxonomy", create_type=False
)
ISIN_BASIS = postgresql.ENUM("filing", "bhavcopy", "index_list", name="xbrl_isin_basis", create_type=False)
QUARANTINE_REASON = postgresql.ENUM(
    "shape_changed", "unsupported_taxonomy", "period_unconfirmed", "implausible_as_of",
    "isin_unresolved", "isin_conflict", "unmapped_element", "unexpected_unit", "unexpected_scale",
    "malformed_value", "conflicting_values",
    name="financial_facts_quarantine_reason", create_type=False,
)  # fmt: skip
ENUMS = (TAXONOMY, ISIN_BASIS, QUARANTINE_REASON)

TABLES = ("financial_filing", "financial_facts_quarantine")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"
FACT_COLUMNS = ["isin", "consolidation", "line_item", "period_start", "period_end", "as_of"]

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind)

    op.add_column(
        "financial_facts",
        sa.Column("rule_version", sa.Text(), nullable=False, server_default=sa.text("'pre-0008'")),
    )
    op.alter_column("financial_facts", "rule_version", server_default=None)
    op.drop_constraint("uq_financial_facts_version", "financial_facts", type_="unique")
    op.create_unique_constraint(
        "uq_financial_facts_version", "financial_facts", [*FACT_COLUMNS, "rule_version"],
        postgresql_nulls_not_distinct=True,
    )  # fmt: skip

    op.create_table(
        "financial_filing",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("isin_basis", ISIN_BASIS, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("scrip_code", sa.Text(), nullable=True),
        sa.Column("consolidation", CONSOLIDATION, nullable=False),
        sa.Column("taxonomy", TAXONOMY, nullable=False),
        sa.Column("reporting_quarter", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("board_meeting_date", sa.Date(), nullable=False),
        sa.Column("facts_written", sa.Integer(), nullable=False),
        sa.Column("facts_quarantined", sa.Integer(), nullable=False),
        sa.Column("dimensional_facts_deferred", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_financial_filing_isin_format"),
        sa.CheckConstraint("period_start <= period_end", name="ck_financial_filing_period_order"),
        sa.CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end", name="ck_financial_filing_as_of_after_period_end"
        ),
        sa.CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date >= board_meeting_date",
            name="ck_financial_filing_as_of_not_before_board_meeting",
        ),
        sa.CheckConstraint(
            "facts_written >= 0 AND facts_quarantined >= 0 AND dimensional_facts_deferred >= 0",
            name="ck_financial_filing_counts_non_negative",
        ),
        sa.CheckConstraint(SHA256, name="ck_financial_filing_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_financial_filing_no_model"),
        sa.UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_financial_filing_version"),
    )
    op.create_index("ix_financial_filing_isin_pit", "financial_filing", ["isin", "as_of"])

    op.create_table(
        "financial_facts_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("xbrl_element", sa.Text(), nullable=True),
        sa.Column("context_ref", sa.Text(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
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
            "(xbrl_element IS NULL) = (context_ref IS NULL)",
            name="ck_financial_facts_quarantine_fact_fields_together",
        ),
        sa.CheckConstraint(SHA256, name="ck_financial_facts_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_financial_facts_quarantine_no_model"),
    )
    op.create_index("ix_financial_facts_quarantine_pit", "financial_facts_quarantine", ["as_of"])

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
    op.drop_constraint("uq_financial_facts_version", "financial_facts", type_="unique")
    op.create_unique_constraint(
        "uq_financial_facts_version", "financial_facts", FACT_COLUMNS, postgresql_nulls_not_distinct=True
    )
    op.drop_column("financial_facts", "rule_version")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind)
