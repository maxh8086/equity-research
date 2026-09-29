"""concall_document, concall_transcript, guidance_claim and guidance_quarantine

Store 2 (CLAUDE.md), append-only. One `concall_document` row per transcript
file, written by the ingest adapter with no model involved; one
`concall_transcript` row per document per extraction
(model version + prompt hash + rule version), one `guidance_claim` row per
verified commitment, and one `guidance_quarantine` row per document or claim
held back. Re-extracting under a new model or a grown hedge lexicon adds rows
at the document's original as_of, never revising the old ones
(docs/temporal-model.md).

Three columns differ from every other store here. `model_version` is NOT NULL,
because these rows exist only because a model read the document, and it names
the weights that answered; `model_requested` names the tier that was asked for,
which is what the loader compares before spending another call. And there is
no `direction` column: whether guidance was raised or lowered is a comparison
between two claims, computed at read time from what was known at `t`.

NOTE ON THE REVISION NUMBER: branch claude/next-session-9036bc carries its own
`0010_screener_export`. Whichever lands second is renumbered to 0011 and has
its `down_revision` pointed at the first; two heads at 0010 will not merge on
their own.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ISSUE_REASON = postgresql.ENUM(
    "shape_changed", "schema_rejected", "isin_unresolved", "isin_conflict", "implausible_as_of",
    "no_text_layer", "quote_not_in_transcript", "quote_ambiguous", "value_not_in_quote",
    "hedge_not_in_quote", "unmapped_hedge", "unmapped_metric", "value_unparsed", "section_mismatch",
    "not_forward_looking",
    name="guidance_issue_reason", create_type=False,
)  # fmt: skip
HEDGE_STRENGTH = postgresql.ENUM(
    "will", "expect", "aim_to", "working_towards", name="hedge_strength", create_type=False
)
CLAIM_SECTION = postgresql.ENUM(
    "prepared_remarks", "qa", "media_interview", name="claim_section", create_type=False
)
CLAIM_SPECIFICITY = postgresql.ENUM(
    "point", "range", "bound", "directional", name="claim_specificity", create_type=False
)
CLAIM_UNIT = postgresql.ENUM(
    "percent", "bps", "inr_crore", "multiple", "count", name="claim_unit", create_type=False
)
NEW_ENUMS = (ISSUE_REASON, HEDGE_STRENGTH, CLAIM_SECTION, CLAIM_SPECIFICITY, CLAIM_UNIT)

TABLES = ("concall_document", "concall_transcript", "guidance_claim", "guidance_quarantine")

XBRL_ISIN_BASIS = postgresql.ENUM(name="xbrl_isin_basis", create_type=False)  # created by 0008

ISIN_FORMAT = "isin ~ '^IN[A-Z0-9]{9}[0-9]$'"
SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"
PROMPT_SHA256 = "prompt_hash ~ '^[0-9a-f]{64}$'"
NOT_BEFORE_CALL = "(timezone('Asia/Kolkata', as_of))::date >= call_date"

# Provenance columns are written out in every table: tests/test_architecture.py
# checks each create_table call as written.


def upgrade() -> None:
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        enum.create(bind)

    op.create_table(
        "concall_document",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("isin_basis", XBRL_ISIN_BASIS, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("call_date", sa.Date(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_concall_document_isin_format"),
        sa.CheckConstraint(NOT_BEFORE_CALL, name="ck_concall_document_as_of_not_before_call"),
        sa.CheckConstraint(
            "char_count > 0 AND page_count >= 0", name="ck_concall_document_counts_non_negative"
        ),
        sa.CheckConstraint(SHA256, name="ck_concall_document_content_hash_sha256"),
        sa.CheckConstraint("model_version IS NULL", name="ck_concall_document_no_model"),
        sa.UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_concall_document_version"),
    )
    op.create_index("ix_concall_document_isin_pit", "concall_document", ["isin", "as_of"])

    op.create_table(
        "concall_transcript",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("call_date", sa.Date(), nullable=False),
        sa.Column("fiscal_period", sa.Text(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("claims_written", sa.Integer(), nullable=False),
        sa.Column("claims_quarantined", sa.Integer(), nullable=False),
        sa.Column("prompt_hash", sa.CHAR(64), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("model_requested", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_concall_transcript_isin_format"),
        sa.CheckConstraint(NOT_BEFORE_CALL, name="ck_concall_transcript_as_of_not_before_call"),
        sa.CheckConstraint(
            "char_count > 0 AND claims_written >= 0 AND claims_quarantined >= 0",
            name="ck_concall_transcript_counts_non_negative",
        ),
        sa.CheckConstraint(SHA256, name="ck_concall_transcript_content_hash_sha256"),
        sa.CheckConstraint(PROMPT_SHA256, name="ck_concall_transcript_prompt_hash_sha256"),
        sa.CheckConstraint("model_version IS NOT NULL", name="ck_concall_transcript_model_named"),
        sa.UniqueConstraint(
            "content_hash", "as_of", "rule_version", "extractor_version", "model_requested",
            "model_version", "prompt_hash",
            name="uq_concall_transcript_version",
        ),  # fmt: skip
    )
    op.create_index("ix_concall_transcript_isin_pit", "concall_transcript", ["isin", "as_of"])

    op.create_table(
        "guidance_claim",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=False),
        sa.Column("call_date", sa.Date(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("metric_verbatim", sa.Text(), nullable=False),
        sa.Column("period_label", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("speaker_name", sa.Text(), nullable=False),
        sa.Column("speaker_role", sa.Text(), nullable=False),
        sa.Column("section", CLAIM_SECTION, nullable=False),
        sa.Column("hedge_verbatim", sa.Text(), nullable=False),
        sa.Column("hedge_strength", HEDGE_STRENGTH, nullable=False),
        sa.Column("specificity", CLAIM_SPECIFICITY, nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_low", sa.Numeric(28, 6), nullable=True),
        sa.Column("value_high", sa.Numeric(28, 6), nullable=True),
        sa.Column("value_unit", CLAIM_UNIT, nullable=False),
        sa.Column("prompt_hash", sa.CHAR(64), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(ISIN_FORMAT, name="ck_guidance_claim_isin_format"),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start", name="ck_guidance_claim_quote_span"
        ),
        sa.CheckConstraint(
            "value_low IS NULL OR value_high IS NULL OR value_low <= value_high",
            name="ck_guidance_claim_value_order",
        ),
        sa.CheckConstraint(
            "(specificity = 'directional') = (value_low IS NULL AND value_high IS NULL)",
            name="ck_guidance_claim_specificity_matches_value",
        ),
        sa.CheckConstraint(
            "(specificity = 'directional') = (value_text IS NULL)",
            name="ck_guidance_claim_specificity_matches_text",
        ),
        sa.CheckConstraint(
            "specificity <> 'point' OR value_low = value_high",
            name="ck_guidance_claim_point_is_one_number",
        ),
        sa.CheckConstraint(
            "specificity <> 'bound' OR (value_low IS NULL) <> (value_high IS NULL)",
            name="ck_guidance_claim_bound_is_one_sided",
        ),
        sa.CheckConstraint(NOT_BEFORE_CALL, name="ck_guidance_claim_as_of_not_before_call"),
        sa.CheckConstraint(SHA256, name="ck_guidance_claim_content_hash_sha256"),
        sa.CheckConstraint(PROMPT_SHA256, name="ck_guidance_claim_prompt_hash_sha256"),
        sa.CheckConstraint("model_version IS NOT NULL", name="ck_guidance_claim_model_named"),
        sa.UniqueConstraint(
            "content_hash", "as_of", "rule_version", "extractor_version", "model_version", "prompt_hash",
            "quote_start", "metric", "period_label",
            name="uq_guidance_claim_version",
        ),  # fmt: skip
    )
    op.create_index("ix_guidance_claim_isin_pit", "guidance_claim", ["isin", "metric", "as_of"])

    op.create_table(
        "guidance_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("isin", sa.CHAR(12), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("reason", ISSUE_REASON, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.CHAR(64), nullable=True),
        sa.Column("extractor_version", sa.Text(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(SHA256, name="ck_guidance_quarantine_content_hash_sha256"),
        sa.CheckConstraint(
            "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
            name="ck_guidance_quarantine_prompt_hash_sha256",
        ),
    )
    op.create_index("ix_guidance_quarantine_pit", "guidance_quarantine", ["as_of"])

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
    bind = op.get_bind()
    for enum in reversed(NEW_ENUMS):
        enum.drop(bind)
