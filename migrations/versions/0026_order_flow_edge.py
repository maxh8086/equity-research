"""order_flow_edge: directed edges in inter-company order graph

Tracks order relationships between companies and identifies cycles. Each edge
represents a company ordering from a counterparty. Used to detect circular
order flows (A orders from B, B orders from C, C orders from A).

Revision ID: 0026
Revises: 0025_rpt_order_flag
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0026"
down_revision: str | None = "0025_rpt_order_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the order_flow_cycle enum
    cycle_enum = pg.ENUM(
        "no_cycle",
        "cycle_2_node",
        "cycle_3_node",
        "cycle_3_plus",
        name="order_flow_cycle",
        create_type=False,
    )
    cycle_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "order_flow_edge",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_isin", sa.CHAR(12), nullable=False),
        sa.Column("target_counterparty", sa.Text(), nullable=False),
        sa.Column("target_isin", sa.CHAR(12), nullable=True),
        sa.Column("order_win_id", sa.BigInteger(), nullable=False),
        sa.Column("announced_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_status", cycle_enum, nullable=False, server_default="no_cycle"),
        sa.Column("cycle_length", sa.Integer(), nullable=True),
        sa.Column("cycle_path", pg.ARRAY(sa.Text()), nullable=True),
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
            "source_isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_order_flow_edge_source_isin_format"
        ),
        sa.CheckConstraint(
            "target_isin IS NULL OR target_isin ~ '^IN[A-Z0-9]{9}[0-9]$'",
            name="ck_order_flow_edge_target_isin_format",
        ),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_order_flow_edge_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_order_flow_edge_no_model"),
        sa.CheckConstraint(
            "(cycle_length IS NULL) = (cycle_path IS NULL)",
            name="ck_order_flow_edge_cycle_len_with_path",
        ),
        sa.CheckConstraint(
            "(cycle_status = 'no_cycle') = (cycle_length IS NULL)",
            name="ck_order_flow_edge_no_cycle_no_len",
        ),
        sa.UniqueConstraint(
            "order_win_id", "rule_version", name="uq_order_flow_edge_win_rule"
        ),
        sa.Index("ix_order_flow_edge_pit", "source_isin", "as_of"),
        sa.Index("ix_order_flow_edge_cycle", "source_isin", "cycle_status"),
    )


def downgrade() -> None:
    op.drop_table("order_flow_edge")
    pg.ENUM(name="order_flow_cycle").drop(op.get_bind(), checkfirst=True)