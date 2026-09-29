"""Fund-raising corporate actions, promoter-allottee flag, demerger milestone event types

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ACTION_TYPES = ("qip", "preferential_allotment", "warrants", "esop", "fccb")
NEW_EVENT_TYPES = (
    "demerger_scheme",
    "demerger_board",
    "demerger_shareholder",
    "demerger_creditor",
    "demerger_nclt_order",
    "demerger_listing",
)

PRELIMINARY = "status IN ('announced', 'approved', 'withdrawn')"
BASE_TERMS = (
    " WHEN 'split' THEN face_value_to < face_value_from AND face_value_to > 0"
    " WHEN 'consolidation' THEN face_value_to > face_value_from AND face_value_from > 0"
    " WHEN 'bonus' THEN shares_new > 0 AND shares_held > 0"
    " WHEN 'rights' THEN shares_new > 0 AND shares_held > 0 AND issue_price >= 0"
    " WHEN 'dividend' THEN dividend_per_share >= 0"
    " WHEN 'demerger' THEN retained_fraction > 0 AND retained_fraction < 1"
    " WHEN 'isin_change' THEN new_isin IS NOT NULL"
)
FUNDRAISING_TERMS = (
    " WHEN 'qip' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
    " WHEN 'preferential_allotment' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
    " WHEN 'warrants' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
    " WHEN 'esop' THEN COALESCE(shares_new > 0, false)"
    " WHEN 'fccb' THEN COALESCE(shares_new > 0, false)"
)


def _terms_constraint(extra: str) -> str:
    return f"{PRELIMINARY} OR CASE action_type{BASE_TERMS}{extra} END"


def upgrade() -> None:
    # ADD VALUE cannot be used in the transaction that adds it, and the new
    # check constraint uses the values, so commit the additions first.
    with op.get_context().autocommit_block():
        for value in NEW_ACTION_TYPES:
            op.execute(f"ALTER TYPE corporate_action_type ADD VALUE IF NOT EXISTS '{value}'")
        for value in NEW_EVENT_TYPES:
            op.execute(f"ALTER TYPE scheduled_event_type ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column("corporate_action", sa.Column("allottee_is_promoter", sa.Boolean(), nullable=True))
    op.drop_constraint("ck_corporate_action_terms", "corporate_action", type_="check")
    op.create_check_constraint(
        "ck_corporate_action_terms", "corporate_action", _terms_constraint(FUNDRAISING_TERMS)
    )


def downgrade() -> None:
    # PostgreSQL cannot remove enum values; they stay after downgrade (as in
    # 0018). The restored constraint leaves the new types unchecked.
    op.drop_constraint("ck_corporate_action_terms", "corporate_action", type_="check")
    op.create_check_constraint("ck_corporate_action_terms", "corporate_action", _terms_constraint(""))
    op.drop_column("corporate_action", "allottee_is_promoter")
