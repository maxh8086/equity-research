"""screener_export and screener_value, append-only

Screener.in Excel exports dropped by hand (ingest/screener_export): the
outside reference the Session 6 validation compares our computed numbers
with. One `screener_export` row per parsed file and rule_version, one
`screener_value` row per non-blank Data Sheet cell, in absolute units.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSOLIDATION = postgresql.ENUM("standalone", "consolidated", name="consolidation", create_type=False)

TABLES = ("screener_export", "screener_value")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    op.create_table(
        "screener_export",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("consolidation", CONSOLIDATION, nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("rows_written", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_screener_export_isin_format"),
        sa.CheckConstraint("rows_written > 0", name="ck_screener_export_rows_written"),
        sa.CheckConstraint(SHA256, name="ck_screener_export_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_screener_export_no_model"),
        sa.UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_screener_export_version"),
    )
    op.create_index("ix_screener_export_isin_pit", "screener_export", ["isin", "consolidation", "as_of"])

    op.create_table(
        "screener_value",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("consolidation", CONSOLIDATION, nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(28, 6), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_screener_value_isin_format"),
        sa.CheckConstraint("statement IN ('pl', 'quarter', 'bs', 'cf')", name="ck_screener_value_statement"),
        sa.CheckConstraint("unit IN ('INR', 'shares', 'INR_per_share')", name="ck_screener_value_unit"),
        sa.CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end", name="ck_screener_value_as_of_after_period_end"
        ),
        sa.CheckConstraint(SHA256, name="ck_screener_value_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_screener_value_no_model"),
        sa.UniqueConstraint(
            "content_hash", "as_of", "rule_version", "statement", "line", "period_end", name="uq_screener_value_version"
        ),
    )
    op.create_index("ix_screener_value_pit", "screener_value", ["isin", "consolidation", "as_of"])

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
