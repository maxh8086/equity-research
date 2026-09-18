"""corporate_action and corporate_action_quarantine, append-only

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTION_TYPE = postgresql.ENUM(
    "split", "consolidation", "bonus", "rights", "dividend", "demerger", "isin_change",
    name="corporate_action_type", create_type=False,
)  # fmt: skip
ACTION_STATUS = postgresql.ENUM(
    "announced", "approved", "dates_set", "effective", "completed", "revised", "withdrawn",
    name="corporate_action_status", create_type=False,
)  # fmt: skip
RATIO_BASIS = postgresql.ENUM(
    "exchange_field", "human_verified", "unverified", name="ratio_basis", create_type=False
)
QUARANTINE_REASON = postgresql.ENUM(
    "shape_changed", "malformed_row", "invalid_isin", "unknown_symbol", "unparsed_purpose",
    "ratio_not_in_exchange_field", "duplicate_action",
    name="corporate_action_quarantine_reason", create_type=False,
)  # fmt: skip
ENUMS = (ACTION_TYPE, ACTION_STATUS, RATIO_BASIS, QUARANTINE_REASON)

TABLES = ("corporate_action", "corporate_action_quarantine")

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"
PRELIMINARY = "status IN ('announced', 'approved', 'withdrawn')"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind)

    op.create_table(
        "corporate_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("action_type", ACTION_TYPE, nullable=False),
        sa.Column("status", ACTION_STATUS, nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("face_value_from", sa.Numeric(20, 4), nullable=True),
        sa.Column("face_value_to", sa.Numeric(20, 4), nullable=True),
        sa.Column("shares_new", sa.Integer(), nullable=True),
        sa.Column("shares_held", sa.Integer(), nullable=True),
        sa.Column("issue_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("dividend_per_share", sa.Numeric(20, 4), nullable=True),
        sa.Column("retained_fraction", sa.Numeric(12, 10), nullable=True),
        sa.Column("new_isin", sa.CHAR(12), nullable=True),
        sa.Column("ratio_basis", RATIO_BASIS, nullable=False),
        sa.Column("verified_by", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_corporate_action_isin_format"),
        sa.CheckConstraint("source_row > 0", name="ck_corporate_action_source_row_positive"),
        sa.CheckConstraint(
            "new_isin IS NULL OR (new_isin ~ '^IN[A-Z0-9]{9}[0-9]$' AND new_isin <> isin)",
            name="ck_corporate_action_new_isin",
        ),
        sa.CheckConstraint(
            "(ratio_basis = 'human_verified') = (verified_by IS NOT NULL)",
            name="ck_corporate_action_verified_by_iff_human",
        ),
        sa.CheckConstraint(f"{PRELIMINARY} OR ex_date IS NOT NULL", name="ck_corporate_action_ex_date_once_set"),
        sa.CheckConstraint(
            f"{PRELIMINARY} OR CASE action_type"
            " WHEN 'split' THEN face_value_to < face_value_from AND face_value_to > 0"
            " WHEN 'consolidation' THEN face_value_to > face_value_from AND face_value_from > 0"
            " WHEN 'bonus' THEN shares_new > 0 AND shares_held > 0"
            " WHEN 'rights' THEN shares_new > 0 AND shares_held > 0 AND issue_price >= 0"
            " WHEN 'dividend' THEN dividend_per_share >= 0"
            " WHEN 'demerger' THEN retained_fraction > 0 AND retained_fraction < 1"
            " WHEN 'isin_change' THEN new_isin IS NOT NULL"
            " END",
            name="ck_corporate_action_terms",
        ),
        sa.CheckConstraint(
            "ex_date IS NULL OR status = 'withdrawn'"
            " OR (as_of AT TIME ZONE 'Asia/Kolkata')::date <= ex_date",
            name="ck_corporate_action_as_of_not_after_ex_date",
        ),
        sa.CheckConstraint(SHA256, name="ck_corporate_action_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_corporate_action_no_model"),
        sa.UniqueConstraint(
            "action_key", "as_of", "content_hash", "rule_version", name="uq_corporate_action_version"
        ),
    )
    op.create_index("ix_corporate_action_isin_pit", "corporate_action", ["isin", "as_of"])
    op.create_index("ix_corporate_action_new_isin_pit", "corporate_action", ["new_isin", "as_of"])

    op.create_table(
        "corporate_action_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=True),
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
            name="ck_corporate_action_quarantine_row_fields_together",
        ),
        sa.CheckConstraint(SHA256, name="ck_corporate_action_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_corporate_action_quarantine_no_model"),
    )
    op.create_index("ix_corporate_action_quarantine_pit", "corporate_action_quarantine", ["as_of"])

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
    for enum in reversed(ENUMS):
        enum.drop(bind)
