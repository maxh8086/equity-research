"""raw_source_file: provenance of raw bytes held in blob storage, append-only

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_source_file",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
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
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_raw_source_file_content_hash_sha256"
        ),
        sa.CheckConstraint(
            "as_of <= fetched_at", name="ck_raw_source_file_as_of_not_after_fetch"
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_raw_source_file_byte_size"),
        sa.CheckConstraint("model_version IS NULL", name="ck_raw_source_file_no_model"),
        sa.UniqueConstraint(
            "content_hash", "source_url", "fetched_at", name="uq_raw_source_file_fetch"
        ),
    )
    op.create_index("ix_raw_source_file_pit", "raw_source_file", ["extracted_by", "as_of"])

    op.execute(
        """
        CREATE TRIGGER raw_source_file_append_only
        BEFORE UPDATE OR DELETE ON raw_source_file
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER raw_source_file_no_truncate
        BEFORE TRUNCATE ON raw_source_file
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("raw_source_file")
