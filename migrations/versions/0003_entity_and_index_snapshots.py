"""entity, entity_isin and index constituent snapshots, append-only

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created explicitly: index_code is shared by two tables.
INDEX_CODE = postgresql.ENUM("nifty_50", "nifty_next_50", name="index_code", create_type=False)
LINK_BASIS = postgresql.ENUM("first_seen", name="entity_link_basis", create_type=False)
QUARANTINE_REASON = postgresql.ENUM(
    "unknown_file",
    "shape_changed",
    "row_count",
    "duplicate_isin",
    "digest_mismatch",
    "malformed_row",
    "invalid_isin",
    "non_equity_series",
    name="index_quarantine_reason",
    create_type=False,
)

TABLES = (
    "entity",
    "entity_isin",
    "index_snapshot",
    "index_snapshot_constituent",
    "index_snapshot_quarantine",
)

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (INDEX_CODE, LINK_BASIS, QUARANTINE_REASON):
        enum.create(bind)

    op.create_table(
        "entity",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(SHA256, name="ck_entity_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_entity_no_model"),
    )

    op.create_table(
        "entity_isin",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("entity_id", sa.BigInteger(), sa.ForeignKey("entity.id"), nullable=False),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("basis", LINK_BASIS, nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_entity_isin_isin_format"),
        sa.CheckConstraint(SHA256, name="ck_entity_isin_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_entity_isin_no_model"),
        sa.UniqueConstraint("isin", "as_of", name="uq_entity_isin_version"),
    )

    op.create_table(
        "index_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("index_code", INDEX_CODE, nullable=False),
        sa.Column("constituent_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_rows", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "constituent_count >= 0 AND quarantined_rows >= 0", name="ck_index_snapshot_counts"
        ),
        sa.CheckConstraint(SHA256, name="ck_index_snapshot_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_index_snapshot_no_model"),
        sa.UniqueConstraint(
            "index_code", "source_url", "as_of", name="uq_index_snapshot_publication"
        ),
    )
    op.create_index("ix_index_snapshot_pit", "index_snapshot", ["index_code", "as_of"])

    op.create_table(
        "index_snapshot_constituent",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("index_snapshot.id"), nullable=False
        ),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_index_snapshot_constituent_isin_format"),
        sa.CheckConstraint(SHA256, name="ck_index_snapshot_constituent_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_index_snapshot_constituent_no_model"),
        sa.UniqueConstraint("snapshot_id", "isin", name="uq_index_snapshot_constituent_isin"),
    )

    op.create_table(
        "index_snapshot_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("index_snapshot.id"), nullable=True
        ),
        sa.Column("index_code", INDEX_CODE, nullable=True),
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
            "(row_number IS NULL) = (snapshot_id IS NULL)",
            name="ck_index_snapshot_quarantine_row_iff_snapshot",
        ),
        sa.CheckConstraint(SHA256, name="ck_index_snapshot_quarantine_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_index_snapshot_quarantine_no_model"),
    )
    op.create_index(
        "ix_index_snapshot_quarantine_pit", "index_snapshot_quarantine", ["as_of"]
    )

    # One ISIN, one entity. A single writer loads these, so a row trigger suffices.
    op.execute(
        """
        CREATE FUNCTION entity_isin_single_entity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM entity_isin WHERE isin = NEW.isin AND entity_id <> NEW.entity_id
            ) THEN
                RAISE EXCEPTION 'ISIN % is already linked to another entity', NEW.isin;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER entity_isin_single_entity
        BEFORE INSERT ON entity_isin
        FOR EACH ROW EXECUTE FUNCTION entity_isin_single_entity()
        """
    )

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
    op.execute("DROP FUNCTION entity_isin_single_entity()")
    bind = op.get_bind()
    for enum in (QUARANTINE_REASON, LINK_BASIS, INDEX_CODE):
        enum.drop(bind)
