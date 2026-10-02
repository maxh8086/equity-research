"""node_run, append-only: one row per node run and how it ended

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors core.db.models.NodeRunOutcome (and the registry's OutcomeKind).
OUTCOMES = (
    "success",
    "no_new_data",
    "no_data",
    "disabled",
    "blocked",
    "quarantined",
    "gate_blocked",
    "look_ahead_refused",
    "raised_watch",
    "raised_review",
    "failed",
)
REASON_REQUIRED = ("no_data", "disabled", "blocked", "quarantined", "gate_blocked")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


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
        "node_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("rows_in", sa.Integer(), nullable=True),
        sa.Column("rows_out", sa.Integer(), nullable=True),
        sa.Column("quarantined", sa.Integer(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"outcome IN ({_in(OUTCOMES)})", name="ck_node_run_outcome"),
        sa.CheckConstraint(
            f"outcome NOT IN ({_in(REASON_REQUIRED)}) OR reason IS NOT NULL",
            name="ck_node_run_reason_required",
        ),
        sa.CheckConstraint("node <> '' AND run_id <> ''", name="ck_node_run_ids_nonempty"),
        sa.CheckConstraint("source_url = 'node://' || node", name="ck_node_run_source_url_is_node"),
        sa.CheckConstraint(
            "model_version IS NULL OR node = 'extract_concall'",
            name="ck_node_run_model_only_extract",
        ),
        sa.CheckConstraint(
            "coalesce(rows_in, 0) >= 0 AND coalesce(rows_out, 0) >= 0"
            " AND coalesce(quarantined, 0) >= 0",
            name="ck_node_run_counts_nonnegative",
        ),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_node_run_content_hash_sha256"),
        sa.UniqueConstraint("node", "run_id", name="uq_node_run_node_run_id"),
    )
    op.create_index("ix_node_run_node_as_of", "node_run", ["node", "as_of"])
    op.create_index("ix_node_run_outcome_as_of", "node_run", ["outcome", "as_of"])
    _append_only("node_run")


def downgrade() -> None:
    op.drop_table("node_run")