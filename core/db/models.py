from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
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


# --------------------------------------------------------------------------- #
# Entities and index membership evidence
# --------------------------------------------------------------------------- #


class IndexCode(StrEnum):
    NIFTY_50 = "nifty_50"
    NIFTY_NEXT_50 = "nifty_next_50"


class EntityLinkBasis(StrEnum):
    FIRST_SEEN = "first_seen"  # ISIN listed by an official source; corporate actions add more


class QuarantineReason(StrEnum):
    # Whole file: nothing from it enters a store
    UNKNOWN_FILE = "unknown_file"
    SHAPE_CHANGED = "shape_changed"
    ROW_COUNT = "row_count"
    DUPLICATE_ISIN = "duplicate_isin"
    DIGEST_MISMATCH = "digest_mismatch"
    # One row: the rest of the file is stored and the snapshot marked incomplete
    MALFORMED_ROW = "malformed_row"
    INVALID_ISIN = "invalid_isin"
    NON_EQUITY_SERIES = "non_equity_series"


INDEX_CODE = _pg_enum(IndexCode, "index_code")


class Entity(ProvenanceMixin, Base):
    """A security's identity across ISIN changes (splits, demergers, amalgamations).

    Every ISIN has its own entity row, and this table is never rewritten when
    an ISIN changes: lineage across a split, demerger or amalgamation is
    computed at read time from `isin_change` corporate actions known at `t`
    (core.db.pit.isin_lineage_as_of). Provenance is the evidence that created the row; what was known
    when is read through `entity_isin` (core.db.pit). Append-only.
    """

    __tablename__ = "entity"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    __table_args__ = (
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_entity_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_entity_no_model"),
    )


class EntityIsin(ProvenanceMixin, Base):
    """An ISIN belongs to an entity, known from `as_of`. Append-only.

    Evidence for an earlier time found later (a 2016 archive capture loaded
    after today's list) appends a row with the earlier `as_of`. A DB trigger
    rejects linking one ISIN to two entities.
    """

    __tablename__ = "entity_isin"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id"), nullable=False)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    basis: Mapped[EntityLinkBasis] = mapped_column(
        _pg_enum(EntityLinkBasis, "entity_link_basis"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_entity_isin_isin_format"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_entity_isin_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_entity_isin_no_model"),
        UniqueConstraint("isin", "as_of", name="uq_entity_isin_version"),
    )


class IndexSnapshot(ProvenanceMixin, Base):
    """One published constituent list for one index, known from `as_of`.

    Membership intervals are computed from snapshots (core.compute.membership),
    never stored. `quarantined_rows > 0` makes the snapshot incomplete: it
    confirms presence but not absence. Append-only.
    """

    __tablename__ = "index_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    index_code: Mapped[IndexCode] = mapped_column(INDEX_CODE, nullable=False)
    constituent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quarantined_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "constituent_count >= 0 AND quarantined_rows >= 0", name="ck_index_snapshot_counts"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_index_snapshot_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_index_snapshot_no_model"),
        # A reparse under a corrected rule_version adds a row rather than editing the
        # old one (append-only); core.db.pit reads pick the newest row per file.
        UniqueConstraint(
            "index_code", "source_url", "as_of", "rule_version", name="uq_index_snapshot_publication"
        ),
        Index("ix_index_snapshot_pit", "index_code", "as_of"),
    )


class IndexSnapshotConstituent(ProvenanceMixin, Base):
    """One row of a constituent list, exactly as published. Append-only."""

    __tablename__ = "index_snapshot_constituent"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("index_snapshot.id"), nullable=False
    )
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    series: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    industry: Mapped[str] = mapped_column(Text, nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_index_snapshot_constituent_isin_format"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_index_snapshot_constituent_content_hash_sha256",
        ),
        CheckConstraint("model_version IS NULL", name="ck_index_snapshot_constituent_no_model"),
        UniqueConstraint("snapshot_id", "isin", name="uq_index_snapshot_constituent_isin"),
    )


class IndexSnapshotQuarantine(ProvenanceMixin, Base):
    """A constituent list or row held back for manual review. Never guessed.

    A whole-file entry has no snapshot; a row entry belongs to the snapshot
    that was stored without it. Append-only.
    """

    __tablename__ = "index_snapshot_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("index_snapshot.id"), nullable=True
    )
    index_code: Mapped[IndexCode | None] = mapped_column(INDEX_CODE, nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_row: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[QuarantineReason] = mapped_column(
        _pg_enum(QuarantineReason, "index_quarantine_reason"), nullable=False
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(row_number IS NULL) = (snapshot_id IS NULL)",
            name="ck_index_snapshot_quarantine_row_iff_snapshot",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_index_snapshot_quarantine_content_hash_sha256",
        ),
        CheckConstraint("model_version IS NULL", name="ck_index_snapshot_quarantine_no_model"),
        Index("ix_index_snapshot_quarantine_pit", "as_of"),
    )


# --------------------------------------------------------------------------- #
# NSE bhavcopy: daily price cross-check and the dated ticker -> ISIN map
# --------------------------------------------------------------------------- #


class BhavcopyQuarantineReason(StrEnum):
    # Whole file: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    ROW_COUNT = "row_count"
    # One row: the rest of the file is stored
    MALFORMED_ROW = "malformed_row"
    INVALID_ISIN = "invalid_isin"
    DUPLICATE_ROW = "duplicate_row"


BHAVCOPY_QUARANTINE_REASON = _pg_enum(BhavcopyQuarantineReason, "bhavcopy_quarantine_reason")


class NseBhavcopyRow(ProvenanceMixin, Base):
    """One equity's row from one day's NSE bhavcopy, exactly as published.

    Only rows whose series is a confirmed equity series are stored (the file
    also carries SME, debt, gilt and gold-bond instruments out of this
    system's scope -- ingest.nse_bhavcopy.parser.EQUITY_SERIES). This is both
    the cross-check for Upstox prices and, via core.db.pit.symbol_to_isin_as_of,
    the dated ticker -> ISIN map: a symbol resolves to whatever ISIN traded
    under it on the nearest bhavcopy on or before a date. Append-only; a
    reparse under a corrected rule_version adds a row rather than editing the
    old one (core.db.pit.bhavcopy_rows_as_of picks the newest per trade_date).
    """

    __tablename__ = "nse_bhavcopy_row"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    series: Mapped[str] = mapped_column(Text, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    prev_close: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnover: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    trades: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_nse_bhavcopy_row_isin_format"),
        CheckConstraint(
            "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0 AND prev_close >= 0",
            name="ck_nse_bhavcopy_row_prices_non_negative",
        ),
        CheckConstraint("high >= low", name="ck_nse_bhavcopy_row_high_not_below_low"),
        CheckConstraint(
            "volume >= 0 AND turnover >= 0 AND trades >= 0", name="ck_nse_bhavcopy_row_counts_non_negative"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_nse_bhavcopy_row_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_nse_bhavcopy_row_no_model"),
        UniqueConstraint(
            "trade_date", "isin", "series", "rule_version", name="uq_nse_bhavcopy_row_publication"
        ),
        Index("ix_nse_bhavcopy_row_isin_pit", "isin", "trade_date"),
        Index("ix_nse_bhavcopy_row_symbol_pit", "symbol", "trade_date"),
    )


class NseBhavcopyQuarantine(ProvenanceMixin, Base):
    """A bhavcopy file or row held back for manual review. Never guessed.

    A whole-file entry has `row_number` NULL. Append-only; superseded once a
    matching `nse_bhavcopy_row` exists for the same file (core.db.pit
    `bhavcopy_quarantine_review_as_of`).
    """

    __tablename__ = "nse_bhavcopy_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    series: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_row: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[BhavcopyQuarantineReason] = mapped_column(BHAVCOPY_QUARANTINE_REASON, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(row_number IS NULL) = (raw_row IS NULL)",
            name="ck_nse_bhavcopy_quarantine_row_fields_together",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_nse_bhavcopy_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_nse_bhavcopy_quarantine_no_model"),
        Index("ix_nse_bhavcopy_quarantine_pit", "as_of"),
    )


# --------------------------------------------------------------------------- #
# Upstox v3 daily candles: the vendor's price history, keyed by ISIN
# --------------------------------------------------------------------------- #


class UpstoxQuarantineReason(StrEnum):
    # Whole response: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    # One candle: the rest of the response is stored
    MALFORMED_CANDLE = "malformed_candle"
    OHLC_INCONSISTENT = "ohlc_inconsistent"
    DUPLICATE_DATE = "duplicate_date"


UPSTOX_QUARANTINE_REASON = _pg_enum(UpstoxQuarantineReason, "upstox_quarantine_reason")


class UpstoxCandle(ProvenanceMixin, Base):
    """One daily candle for one ISIN, exactly as Upstox returned it.

    `as_of` is the fetch time, not the trade date: a vendor history may be
    revised after the fact (e.g. adjusted for a later split), so a response is
    only known to be the vendor's view from the moment it was received (R2).
    Whether these are the exchange's as-traded prices is checked against
    `nse_bhavcopy_row` (core.db.pit.upstox_bhavcopy_crosscheck_as_of), never
    assumed. A refetch that overlaps adds rows with a later `as_of`;
    core.db.pit.upstox_candles_as_of picks the newest per trade_date. Append-only.
    """

    __tablename__ = "upstox_candle"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    instrument_key: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_upstox_candle_isin_format"),
        CheckConstraint(
            "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0",
            name="ck_upstox_candle_prices_non_negative",
        ),
        CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ck_upstox_candle_ohlc_consistent",
        ),
        CheckConstraint("volume >= 0 AND open_interest >= 0", name="ck_upstox_candle_counts_non_negative"),
        # A candle cannot be known before its own trading day (IST).
        CheckConstraint(
            "(as_of AT TIME ZONE 'Asia/Kolkata')::date >= trade_date", name="ck_upstox_candle_as_of_after_trade"
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_upstox_candle_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_upstox_candle_no_model"),
        UniqueConstraint(
            "isin", "trade_date", "as_of", "rule_version", name="uq_upstox_candle_publication"
        ),
        Index("ix_upstox_candle_isin_pit", "isin", "trade_date"),
    )


class UpstoxCandleQuarantine(ProvenanceMixin, Base):
    """An Upstox response or candle held back for manual review. Never guessed.

    A whole-response entry has `candle_index` NULL. Append-only; resolved once
    a matching `upstox_candle` exists for the same response (core.db.pit
    `upstox_quarantine_review_as_of`).
    """

    __tablename__ = "upstox_candle_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    candle_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_candle: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[UpstoxQuarantineReason] = mapped_column(UPSTOX_QUARANTINE_REASON, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(candle_index IS NULL) = (raw_candle IS NULL)",
            name="ck_upstox_candle_quarantine_candle_fields_together",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_upstox_candle_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_upstox_candle_quarantine_no_model"),
        Index("ix_upstox_candle_quarantine_pit", "as_of"),
    )


# --------------------------------------------------------------------------- #
# Corporate actions: price adjustment factors and ISIN lineage (store 15, core)
# --------------------------------------------------------------------------- #


class CorporateActionType(StrEnum):
    SPLIT = "split"  # face value sub-division
    CONSOLIDATION = "consolidation"  # face value consolidation (reverse split)
    BONUS = "bonus"
    RIGHTS = "rights"
    DIVIDEND = "dividend"
    DEMERGER = "demerger"
    ISIN_CHANGE = "isin_change"


class CorporateActionStatus(StrEnum):
    ANNOUNCED = "announced"
    APPROVED = "approved"
    DATES_SET = "dates_set"
    EFFECTIVE = "effective"
    COMPLETED = "completed"
    REVISED = "revised"  # this version carries revised terms
    WITHDRAWN = "withdrawn"


class RatioBasis(StrEnum):
    EXCHANGE_FIELD = "exchange_field"  # parsed by code from an exchange's own field
    HUMAN_VERIFIED = "human_verified"  # entered or checked by a named person against the evidence
    UNVERIFIED = "unverified"  # e.g. extracted from a PDF: adjusts no price until verified


class CorporateActionQuarantineReason(StrEnum):
    # Whole file: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    # One row: the rest of the file is stored
    MALFORMED_ROW = "malformed_row"
    INVALID_ISIN = "invalid_isin"
    UNKNOWN_SYMBOL = "unknown_symbol"
    UNPARSED_PURPOSE = "unparsed_purpose"
    RATIO_NOT_IN_EXCHANGE_FIELD = "ratio_not_in_exchange_field"
    DUPLICATE_ACTION = "duplicate_action"


CORPORATE_ACTION_TYPE = _pg_enum(CorporateActionType, "corporate_action_type")
CORPORATE_ACTION_STATUS = _pg_enum(CorporateActionStatus, "corporate_action_status")
RATIO_BASIS = _pg_enum(RatioBasis, "ratio_basis")
CORPORATE_ACTION_QUARANTINE_REASON = _pg_enum(
    CorporateActionQuarantineReason, "corporate_action_quarantine_reason"
)

# Statuses under which terms and an ex-date may still be missing.
PRELIMINARY_STATUSES = ("announced", "approved", "withdrawn")


class CorporateAction(ProvenanceMixin, Base):
    """One version of one corporate action, as known from `as_of`.

    `action_key` identifies the action across versions; a later version
    (new status, revised terms, a verified ratio) is a new row, and
    core.db.pit reads take the newest per key: latest `as_of`, then highest
    id. Terms are stored as the source states them (face values, share
    ratios, prices), never as a precomputed factor: factors are computed by
    core.compute.adjustment at read time. Only `exchange_field` and
    `human_verified` ratios adjust prices. `ex_date` is the event time.
    Append-only.
    """

    __tablename__ = "corporate_action"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    action_type: Mapped[CorporateActionType] = mapped_column(CORPORATE_ACTION_TYPE, nullable=False)
    status: Mapped[CorporateActionStatus] = mapped_column(CORPORATE_ACTION_STATUS, nullable=False)
    ex_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    face_value_from: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    face_value_to: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    shares_new: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    dividend_per_share: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    retained_fraction: Mapped[Decimal | None] = mapped_column(Numeric(12, 10), nullable=True)
    new_isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    ratio_basis: Mapped[RatioBasis] = mapped_column(RATIO_BASIS, nullable=False)
    verified_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based data row in the file
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_corporate_action_isin_format"),
        CheckConstraint("source_row > 0", name="ck_corporate_action_source_row_positive"),
        CheckConstraint(
            "new_isin IS NULL OR (new_isin ~ '^IN[A-Z0-9]{9}[0-9]$' AND new_isin <> isin)",
            name="ck_corporate_action_new_isin",
        ),
        CheckConstraint(
            "(ratio_basis = 'human_verified') = (verified_by IS NOT NULL)",
            name="ck_corporate_action_verified_by_iff_human",
        ),
        CheckConstraint(
            "status IN ('announced', 'approved', 'withdrawn') OR ex_date IS NOT NULL",
            name="ck_corporate_action_ex_date_once_set",
        ),
        CheckConstraint(
            "status IN ('announced', 'approved', 'withdrawn') OR CASE action_type"
            " WHEN 'split' THEN face_value_to < face_value_from AND face_value_to > 0"
            " WHEN 'consolidation' THEN face_value_to > face_value_from AND face_value_from > 0"
            " WHEN 'bonus' THEN shares_new > 0 AND shares_held > 0"
            " WHEN 'rights' THEN shares_new > 0 AND shares_held > 0 AND issue_price >= 0"
            " WHEN 'dividend' THEN dividend_per_share >= 0"
            " WHEN 'demerger' THEN retained_fraction > 0 AND retained_fraction < 1"
            " WHEN 'isin_change' THEN new_isin IS NOT NULL"
            " END",
            name="ck_corporate_action_terms",
        ),
        # An ex-date is announced before it arrives (docs/temporal-model.md).
        CheckConstraint(
            "ex_date IS NULL OR status = 'withdrawn'"
            " OR (as_of AT TIME ZONE 'Asia/Kolkata')::date <= ex_date",
            name="ck_corporate_action_as_of_not_after_ex_date",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_corporate_action_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_corporate_action_no_model"),
        UniqueConstraint(
            "action_key", "as_of", "content_hash", "rule_version", name="uq_corporate_action_version"
        ),
        Index("ix_corporate_action_isin_pit", "isin", "as_of"),
        Index("ix_corporate_action_new_isin_pit", "new_isin", "as_of"),
    )


class CorporateActionQuarantine(ProvenanceMixin, Base):
    """A corporate-action file or row held back for manual review. Never guessed.

    A whole-file entry has `row_number` NULL. Append-only; see
    core.db.pit.corporate_action_quarantine_review_as_of.
    """

    __tablename__ = "corporate_action_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_row: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[CorporateActionQuarantineReason] = mapped_column(
        CORPORATE_ACTION_QUARANTINE_REASON, nullable=False
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(row_number IS NULL) = (raw_row IS NULL)",
            name="ck_corporate_action_quarantine_row_fields_together",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_corporate_action_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_corporate_action_quarantine_no_model"),
        Index("ix_corporate_action_quarantine_pit", "as_of"),
    )
