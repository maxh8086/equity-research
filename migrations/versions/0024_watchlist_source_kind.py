"""watchlist_entry: opened_by_kind, opened_by_ref, nullable opened_by_signal_id

Drift and order-win special situations can now raise a stored watchlist entry.
Existing rows are all signal-opened; ADD COLUMN with a constant default fills them
without an UPDATE, so the append-only trigger is not involved and nothing is inferred.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND = postgresql.ENUM(
    "technical_signal",
    "drift",
    "order_win",
    name="watchlist_opened_by_kind",
    create_type=False,
)


def upgrade() -> None:
    KIND.create(op.get_bind())
    op.add_column(
        "watchlist_entry",
        sa.Column(
            "opened_by_kind", KIND, nullable=False, server_default="technical_signal"
        ),
    )
    op.add_column("watchlist_entry", sa.Column("opened_by_ref", sa.Text(), nullable=True))
    op.alter_column("watchlist_entry", "opened_by_signal_id", nullable=True)
    op.create_check_constraint(
        "ck_watchlist_entry_signal_iff_kind",
        "watchlist_entry",
        "(opened_by_kind = 'technical_signal') = (opened_by_signal_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_watchlist_entry_ref_for_special",
        "watchlist_entry",
        "opened_by_kind = 'technical_signal' OR opened_by_ref IS NOT NULL",
    )


def downgrade() -> None:
    other = op.get_bind().execute(
        sa.text("SELECT count(*) FROM watchlist_entry WHERE opened_by_kind <> 'technical_signal'")
    ).scalar_one()
    if other:
        raise RuntimeError(
            f"{other} watchlist_entry rows were opened by drift or order-win; the table is "
            "append-only and they cannot be removed, so 0024 cannot be downgraded"
        )
    op.drop_constraint("ck_watchlist_entry_ref_for_special", "watchlist_entry", type_="check")
    op.drop_constraint("ck_watchlist_entry_signal_iff_kind", "watchlist_entry", type_="check")
    op.alter_column("watchlist_entry", "opened_by_signal_id", nullable=False)
    op.drop_column("watchlist_entry", "opened_by_ref")
    op.drop_column("watchlist_entry", "opened_by_kind")
    KIND.drop(op.get_bind())