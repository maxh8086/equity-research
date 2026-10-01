"""company_event_type: order_flow_cycle

Adds one value to the company_event_type enum for the order flow cycle
detection red flag. No table changes; company_event stays append-only.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALUE = "order_flow_cycle"


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block on older Postgres and the new
    # value cannot be used in the transaction that adds it (precedent: 0019).
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE company_event_type ADD VALUE IF NOT EXISTS '{VALUE}'")


def downgrade() -> None:
    # Postgres cannot drop an enum value, so the value stays in the type. Rows that use
    # it are append-only and cannot be removed either; an unused value is harmless, and
    # the application enum simply no longer names it.
    pass
