"""ingest_row_quarantine, append-only: one generic durable quarantine for adapters without their own

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _append_only(table: str) -> None:
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


def upgrade() -> None:
    op.create_table(
        "ingest_row_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("store", sa.Text(), nullable=False),
        sa.Column("row_ref", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("raw_excerpt", sa.Text(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "adapter <> '' AND store <> '' AND row_ref <> '' AND reason <> ''",
            name="ck_ingest_row_quarantine_keys_nonempty",
        ),
        sa.CheckConstraint(
            "raw_excerpt IS NULL OR char_length(raw_excerpt) <= 2000",
            name="ck_ingest_row_quarantine_excerpt_bounded",
        ),
        sa.CheckConstraint("model_version IS NULL", name="ck_ingest_row_quarantine_no_model"),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_ingest_row_quarantine_content_hash_sha256"
        ),
        sa.UniqueConstraint(
            "adapter", "store", "row_ref", "reason", "rule_version",
            name="uq_ingest_row_quarantine_row_reason_rule",
        ),
    )
    op.create_index(
        "ix_ingest_row_quarantine_adapter_as_of", "ingest_row_quarantine", ["adapter", "as_of"]
    )
    op.create_index("ix_ingest_row_quarantine_pit", "ingest_row_quarantine", ["as_of"])
    _append_only("ingest_row_quarantine")


def downgrade() -> None:
    op.drop_table("ingest_row_quarantine")