from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    CHAR,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from core.db.base import Base, ProvenanceMixin
from core.timezones import require_aware


class Consolidation(StrEnum):
    STANDALONE = "standalone"
    CONSOLIDATED = "consolidated"


class FactKind(StrEnum):
    REPORTED = "reported"  # parsed from an XBRL element
    COMPUTED = "computed"  # derived by core.compute from reported facts


def _pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class FinancialFact(ProvenanceMixin, Base):
    """Store ①: XBRL line items, computed ratios, durability metrics.

    Money is stored in absolute units of `unit` (INR, not crores/lakhs).
    Duration facts (P&L, cash flow) have period_start; instant facts (balance
    sheet) have period_start NULL. Append-only, enforced by a DB trigger.
    """

    __tablename__ = "financial_facts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    consolidation: Mapped[Consolidation] = mapped_column(
        _pg_enum(Consolidation, "consolidation"), nullable=False
    )
    fact_kind: Mapped[FactKind] = mapped_column(_pg_enum(FactKind, "fact_kind"), nullable=False)
    line_item: Mapped[str] = mapped_column(Text, nullable=False)
    xbrl_element: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_financial_facts_isin_format"
        ),
        CheckConstraint(
            "period_start IS NULL OR period_start <= period_end",
            name="ck_financial_facts_period_order",
        ),
        # A fact about a period cannot be known before the period has closed (IST).
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end",
            name="ck_financial_facts_as_of_after_period_end",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_financial_facts_content_hash_sha256"
        ),
        CheckConstraint(
            "(fact_kind = 'reported') = (xbrl_element IS NOT NULL)",
            name="ck_financial_facts_xbrl_element_iff_reported",
        ),
        # R1: numbers that exist in XBRL are parsed, and ratios are computed.
        # No model ever writes to this table.
        CheckConstraint("model_version IS NULL", name="ck_financial_facts_no_model"),
        UniqueConstraint(
            "isin",
            "consolidation",
            "line_item",
            "period_start",
            "period_end",
            "as_of",
            name="uq_financial_facts_version",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_financial_facts_pit",
            "isin",
            "consolidation",
            "line_item",
            "period_end",
            "as_of",
        ),
    )


class RawSourceFile(ProvenanceMixin, Base):
    """A raw source file as fetched or dropped, recorded before anything parses it.

    The bytes live in blob storage under `content_hash`. This row records where
    they came from, when the source published them (`as_of`) and when this
    system received them (`fetched_at`). After a parser fix, the parser re-reads
    stored bytes instead of downloading them again. Append-only.
    """

    __tablename__ = "raw_source_file"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_raw_source_file_content_hash_sha256"
        ),
        # Nothing can have been published after we already held it.
        CheckConstraint("as_of <= fetched_at", name="ck_raw_source_file_as_of_not_after_fetch"),
        CheckConstraint("byte_size >= 0", name="ck_raw_source_file_byte_size"),
        CheckConstraint("model_version IS NULL", name="ck_raw_source_file_no_model"),
        UniqueConstraint(
            "content_hash", "source_url", "fetched_at", name="uq_raw_source_file_fetch"
        ),
        Index("ix_raw_source_file_pit", "extracted_by", "as_of"),
    )

    @validates("fetched_at")
    def _validate_fetched_at(self, key: str, value: datetime) -> datetime:
        return require_aware(value, key)
