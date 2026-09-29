"""rating_action table and supporting enums

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "rating_action"

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"

RATING_AGENCY = postgresql.ENUM(
    "crisil",
    "icra",
    "care",
    "india_ratings",
    "brickwork",
    "acuite",
    name="rating_agency",
    create_type=False,
)

COMMON_RATING_SCALE = postgresql.ENUM(
    "aaa",
    "aa_plus",
    "aa",
    "aa_minus",
    "a_plus",
    "a",
    "a_minus",
    "bbb_plus",
    "bbb",
    "bbb_minus",
    "bb_plus",
    "bb",
    "bb_minus",
    "b_plus",
    "b",
    "b_minus",
    "c",
    "d",
    "withdrawn",
    "suspended",
    "not_rated",
    name="common_rating_scale",
    create_type=False,
)

RATING_OUTLOOK = postgresql.ENUM(
    "stable",
    "positive",
    "negative",
    "watch_positive",
    "watch_negative",
    "developing",
    name="rating_outlook",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    RATING_AGENCY.create(bind)
    COMMON_RATING_SCALE.create(bind)
    RATING_OUTLOOK.create(bind)

    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("agency", RATING_AGENCY, nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("raw_rating", sa.Text(), nullable=False),
        sa.Column("common_scale", COMMON_RATING_SCALE, nullable=False),
        sa.Column("outlook", RATING_OUTLOOK, nullable=True),
        sa.Column("action_date", sa.Date(), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("is_upgrade", sa.Boolean(), nullable=True),
        sa.Column("is_downgrade", sa.Boolean(), nullable=True),
        sa.Column("is_withdrawn", sa.Boolean(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        # Provenance columns (ProvenanceMixin)
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
        sa.CheckConstraint(ISIN_FORMAT, name="ck_rating_action_isin_format"),
        sa.CheckConstraint("model_version IS NULL", name="ck_rating_action_no_model"),
        sa.CheckConstraint(
            "evidence_url IS NOT NULL AND evidence_url != ''",
            name="ck_rating_action_evidence_url_required",
        ),
        sa.CheckConstraint(
            "severity BETWEEN 1 AND 5", name="ck_rating_action_severity_range"
        ),
        sa.CheckConstraint(SHA256, name="ck_rating_action_content_hash_sha256"),
    )
    op.create_index("ix_rating_action_isin_date", TABLE, ["isin", "action_date"])

    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_append_only
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_truncate
        BEFORE TRUNCATE ON {TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table(TABLE)
    bind = op.get_bind()
    RATING_OUTLOOK.drop(bind)
    COMMON_RATING_SCALE.drop(bind)
    RATING_AGENCY.drop(bind)
