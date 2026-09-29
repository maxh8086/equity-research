"""shareholding_filing, shareholding_pattern and shareholding_quarantine, append-only

The shareholding-pattern XBRL parser (ingest/nse_shp) writes one
`shareholding_filing` row per parsed file and one `shareholding_pattern` row
per (category, measure) count. A revised filing is a new file with a later
as_of; a corrected mapping reparses stored bytes and adds rows under a new
rule_version, dated at the file's original as_of (docs/temporal-model.md).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ISIN_BASIS = postgresql.ENUM("filing", "bhavcopy", "index_list", name="xbrl_isin_basis", create_type=False)
QUARANTINE_REASON = postgresql.ENUM(
    "shape_changed", "unsupported_taxonomy", "implausible_as_of", "isin_unresolved", "isin_conflict",
    "totals_mismatch", "unmapped_element", "unmapped_category", "unexpected_unit", "malformed_value",
    "conflicting_values",
    name="shareholding_quarantine_reason", create_type=False,
)  # fmt: skip

TABLES = ("shareholding_filing", "shareholding_pattern", "shareholding_quarantine")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"
AFTER_AS_ON_DATE = "(timezone('Asia/Kolkata', as_of))::date > as_on_date"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    QUARANTINE_REASON.create(op.get_bind())

    op.create_table(
        "shareholding_filing",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("isin_basis", ISIN_BASIS, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("scrip_code", sa.Text(), nullable=True),
        sa.Column("as_on_date", sa.Date(), nullable=False),
        sa.Column("allotment_date", sa.Date(), nullable=True),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("rows_written", sa.Integer(), nullable=False),
        sa.Column("facts_quarantined", sa.Integer(), nullable=False),
        sa.Column("typed_facts_deferred", sa.Integer(), nullable=False),
        sa.Column("percentage_facts_skipped", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_shareholding_filing_isin_format"),
        sa.CheckConstraint(AFTER_AS_ON_DATE, name="ck_shareholding_filing_as_of_after_as_on_date"),
        sa.CheckConstraint(
            "rows_written >= 0 AND facts_quarantined >= 0 AND typed_facts_deferred >= 0"
            " AND percentage_facts_skipped >= 0",
            name="ck_shareholding_filing_counts_non_negative",
        ),
        sa.CheckConstraint(SHA256, name="ck_shareholding_filing_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_shareholding_filing_no_model"),
        sa.UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_shareholding_filing_version"),
    )
    op.create_index("ix_shareholding_filing_isin_pit", "shareholding_filing", ["isin", "as_of"])

    op.create_table(
        "shareholding_pattern",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("as_on_date", sa.Date(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("parent_category", sa.Text(), nullable=True),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("xbrl_element", sa.Text(), nullable=False),
        sa.Column("xbrl_member", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_shareholding_pattern_isin_format"),
        sa.CheckConstraint("value >= 0", name="ck_shareholding_pattern_value_non_negative"),
        sa.CheckConstraint(AFTER_AS_ON_DATE, name="ck_shareholding_pattern_as_of_after_as_on_date"),
        sa.CheckConstraint(SHA256, name="ck_shareholding_pattern_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_shareholding_pattern_no_model"),
        sa.UniqueConstraint(
            "content_hash", "as_of", "rule_version", "category", "measure", name="uq_shareholding_pattern_version"
        ),
    )
    op.create_index(
        "ix_shareholding_pattern_isin_pit", "shareholding_pattern", ["isin", "as_on_date", "as_of"]
    )

    op.create_table(
        "shareholding_quarantine",
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
            name="ck_shareholding_quarantine_fact_fields_together",
        ),
        sa.CheckConstraint(SHA256, name="ck_shareholding_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_shareholding_quarantine_no_model"),
    )
    op.create_index("ix_shareholding_quarantine_pit", "shareholding_quarantine", ["as_of"])

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
    QUARANTINE_REASON.drop(op.get_bind())
