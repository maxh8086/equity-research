"""index_snapshot: let a reparse under a corrected rule_version add a new row

A file already turned into a snapshot could not previously be re-examined
after a parser fix (only whole-file quarantine could). Widening the
uniqueness key lets `reparse` (ingest/nse_indices/adapters.py) add a
corrected snapshot for the same file, dated at its true original as_of; the
old row is never touched (append-only). Reads pick the newest per file
(core/db/pit.py: index_snapshots_as_of).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_index_snapshot_publication", "index_snapshot", type_="unique")
    op.create_unique_constraint(
        "uq_index_snapshot_publication",
        "index_snapshot",
        ["index_code", "source_url", "as_of", "rule_version"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_index_snapshot_publication", "index_snapshot", type_="unique")
    op.create_unique_constraint(
        "uq_index_snapshot_publication", "index_snapshot", ["index_code", "source_url", "as_of"]
    )
