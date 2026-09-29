from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, validates

from core.compute.guidance import ClaimSection, HedgeStrength, Specificity, Unit
from core.compute.ratings import CommonRatingScale, RatingAgency, RatingOutlook
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
    # Rows written before migration 0008 carry 'pre-0008'.
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

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
            "rule_version",
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
    MSCI_EM = "msci_em"
    MSCI_INDIA = "msci_india"
    FTSE_ALL_WORLD = "ftse_all_world"
    BSE_SENSEX = "bse_sensex"


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
    # Fund-raising: `shares_new` is the shares issued, or issuable on conversion
    # (warrants, ESOPs, FCCBs), so dilution counts them before they convert.
    QIP = "qip"
    PREFERENTIAL_ALLOTMENT = "preferential_allotment"
    WARRANTS = "warrants"
    ESOP = "esop"
    FCCB = "fccb"


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
    # Fund-raising only: NULL when the source does not say who is allotted.
    allottee_is_promoter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
            " WHEN 'qip' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
            " WHEN 'preferential_allotment' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
            " WHEN 'warrants' THEN COALESCE(shares_new > 0 AND issue_price > 0, false)"
            " WHEN 'esop' THEN COALESCE(shares_new > 0, false)"
            " WHEN 'fccb' THEN COALESCE(shares_new > 0, false)"
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


class XbrlTaxonomy(StrEnum):
    IND_AS = "ind_as"
    NBFC = "nbfc"
    BANK = "bank"
    LIFE_INSURANCE = "life_insurance"
    GENERAL_INSURANCE = "general_insurance"


class XbrlIsinBasis(StrEnum):
    FILING = "filing"  # the ISIN fact inside the XBRL file (banks, insurers)
    BHAVCOPY = "bhavcopy"  # dated symbol -> ISIN map from NSE bhavcopy rows
    INDEX_LIST = "index_list"  # symbol -> ISIN from an index constituent list


class FinancialFactsQuarantineReason(StrEnum):
    # Whole file: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    UNSUPPORTED_TAXONOMY = "unsupported_taxonomy"
    PERIOD_UNCONFIRMED = "period_unconfirmed"
    IMPLAUSIBLE_AS_OF = "implausible_as_of"
    ISIN_UNRESOLVED = "isin_unresolved"
    ISIN_CONFLICT = "isin_conflict"
    # One fact: the rest of the file is stored
    UNMAPPED_ELEMENT = "unmapped_element"
    UNEXPECTED_UNIT = "unexpected_unit"
    UNEXPECTED_SCALE = "unexpected_scale"
    MALFORMED_VALUE = "malformed_value"
    CONFLICTING_VALUES = "conflicting_values"


XBRL_TAXONOMY = _pg_enum(XbrlTaxonomy, "xbrl_taxonomy")
XBRL_ISIN_BASIS = _pg_enum(XbrlIsinBasis, "xbrl_isin_basis")
FINANCIAL_FACTS_QUARANTINE_REASON = _pg_enum(
    FinancialFactsQuarantineReason, "financial_facts_quarantine_reason"
)


class FinancialFiling(ProvenanceMixin, Base):
    """One XBRL results file as parsed under one rule_version.

    `period_start`/`period_end` are the filing's current-quarter column; its
    facts in `financial_facts` share this row's `content_hash` and `as_of`.
    A reparse under a corrected rule adds a row. Append-only.
    """

    __tablename__ = "financial_filing"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    isin_basis: Mapped[XbrlIsinBasis] = mapped_column(XBRL_ISIN_BASIS, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    scrip_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    consolidation: Mapped[Consolidation] = mapped_column(
        _pg_enum(Consolidation, "consolidation"), nullable=False
    )
    taxonomy: Mapped[XbrlTaxonomy] = mapped_column(XBRL_TAXONOMY, nullable=False)
    reporting_quarter: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    board_meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    facts_written: Mapped[int] = mapped_column(Integer, nullable=False)
    facts_quarantined: Mapped[int] = mapped_column(Integer, nullable=False)
    # Segment and other dimensional facts, not parsed yet; a later rule reparses from blob.
    dimensional_facts_deferred: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_financial_filing_isin_format"),
        CheckConstraint("period_start <= period_end", name="ck_financial_filing_period_order"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end",
            name="ck_financial_filing_as_of_after_period_end",
        ),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date >= board_meeting_date",
            name="ck_financial_filing_as_of_not_before_board_meeting",
        ),
        CheckConstraint(
            "facts_written >= 0 AND facts_quarantined >= 0 AND dimensional_facts_deferred >= 0",
            name="ck_financial_filing_counts_non_negative",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_financial_filing_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_financial_filing_no_model"),
        UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_financial_filing_version"),
        Index("ix_financial_filing_isin_pit", "isin", "as_of"),
    )


class FinancialFactsQuarantine(ProvenanceMixin, Base):
    """An XBRL file or fact held back for manual review. Never guessed.

    A whole-file entry has `xbrl_element` and `context_ref` NULL. Append-only;
    see core.db.pit.financial_facts_quarantine_review_as_of.
    """

    __tablename__ = "financial_facts_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    xbrl_element: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[FinancialFactsQuarantineReason] = mapped_column(
        FINANCIAL_FACTS_QUARANTINE_REASON, nullable=False
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(xbrl_element IS NULL) = (context_ref IS NULL)",
            name="ck_financial_facts_quarantine_fact_fields_together",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_financial_facts_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_financial_facts_quarantine_no_model"),
        Index("ix_financial_facts_quarantine_pit", "as_of"),
    )


class ShareholdingQuarantineReason(StrEnum):
    # Whole file: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    UNSUPPORTED_TAXONOMY = "unsupported_taxonomy"
    IMPLAUSIBLE_AS_OF = "implausible_as_of"
    ISIN_UNRESOLVED = "isin_unresolved"
    ISIN_CONFLICT = "isin_conflict"
    TOTALS_MISMATCH = "totals_mismatch"  # a category is not the sum of its sub-categories
    # One fact: the rest of the file is stored
    UNMAPPED_ELEMENT = "unmapped_element"
    UNMAPPED_CATEGORY = "unmapped_category"
    UNEXPECTED_UNIT = "unexpected_unit"
    MALFORMED_VALUE = "malformed_value"
    CONFLICTING_VALUES = "conflicting_values"


SHAREHOLDING_QUARANTINE_REASON = _pg_enum(ShareholdingQuarantineReason, "shareholding_quarantine_reason")


class ShareholdingFiling(ProvenanceMixin, Base):
    """One shareholding-pattern XBRL file as parsed under one rule_version.

    `as_on_date` is the date the pattern describes (a quarter end, or the
    allotment date of an off-cycle filing). A revised filing is another file
    with a later `as_of`; a reparse under a corrected rule adds a row. Append-only.
    """

    __tablename__ = "shareholding_filing"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    isin_basis: Mapped[XbrlIsinBasis] = mapped_column(XBRL_ISIN_BASIS, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    scrip_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    as_on_date: Mapped[date] = mapped_column(Date, nullable=False)
    allotment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    taxonomy_version: Mapped[str] = mapped_column(Text, nullable=False)
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False)
    facts_quarantined: Mapped[int] = mapped_column(Integer, nullable=False)
    # Named holders and other typed-dimension facts, not parsed yet; a later rule reparses from blob.
    typed_facts_deferred: Mapped[int] = mapped_column(Integer, nullable=False)
    percentage_facts_skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_shareholding_filing_isin_format"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > as_on_date",
            name="ck_shareholding_filing_as_of_after_as_on_date",
        ),
        CheckConstraint(
            "rows_written >= 0 AND facts_quarantined >= 0 AND typed_facts_deferred >= 0"
            " AND percentage_facts_skipped >= 0",
            name="ck_shareholding_filing_counts_non_negative",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_shareholding_filing_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_shareholding_filing_no_model"),
        UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_shareholding_filing_version"),
        Index("ix_shareholding_filing_isin_pit", "isin", "as_of"),
    )


class ShareholdingPattern(ProvenanceMixin, Base):
    """Store 16: one count for one shareholder category, from one filing.

    Long format: (category, measure) -> value, e.g. ("promoter_group",
    "pledged_shares"). Categories and measures are keys from
    ingest/nse_shp/mapping.py; `parent_category` is the category this one
    rolls up into. Counts only; percentages are computed by code. Rows share
    their filing's content_hash, as_of and rule_version. Append-only.
    """

    __tablename__ = "shareholding_pattern"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    as_on_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    parent_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    measure: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    xbrl_element: Mapped[str] = mapped_column(Text, nullable=False)
    xbrl_member: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_shareholding_pattern_isin_format"),
        CheckConstraint("value >= 0", name="ck_shareholding_pattern_value_non_negative"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > as_on_date",
            name="ck_shareholding_pattern_as_of_after_as_on_date",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_shareholding_pattern_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_shareholding_pattern_no_model"),
        UniqueConstraint(
            "content_hash", "as_of", "rule_version", "category", "measure", name="uq_shareholding_pattern_version"
        ),
        Index("ix_shareholding_pattern_isin_pit", "isin", "as_on_date", "as_of"),
    )


class ShareholdingQuarantine(ProvenanceMixin, Base):
    """A shareholding-pattern file or fact held back for manual review. Never guessed.

    A whole-file entry has `xbrl_element` and `context_ref` NULL. Append-only;
    see core.db.pit.shareholding_quarantine_review_as_of.
    """

    __tablename__ = "shareholding_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    xbrl_element: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[ShareholdingQuarantineReason] = mapped_column(SHAREHOLDING_QUARANTINE_REASON, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(xbrl_element IS NULL) = (context_ref IS NULL)",
            name="ck_shareholding_quarantine_fact_fields_together",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_shareholding_quarantine_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_shareholding_quarantine_no_model"),
        Index("ix_shareholding_quarantine_pit", "as_of"),
    )


# --------------------------------------------------------------------------- #
# Screener.in exports: the outside reference for validation (Session 6)
# --------------------------------------------------------------------------- #


class ScreenerExport(ProvenanceMixin, Base):
    """One Screener Excel export as parsed under one rule_version.

    Reference data for validating our computed numbers, never an input to
    them: nothing in financial_facts or any ratio reads it. `as_of` is the
    export time, so each value is what Screener showed then, restatements
    included. `isin` comes from the sidecar and is cross-checked against the
    URL's symbol in the constituent lists. Append-only.
    """

    __tablename__ = "screener_export"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    consolidation: Mapped[Consolidation] = mapped_column(_pg_enum(Consolidation, "consolidation"), nullable=False)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[str] = mapped_column(Text, nullable=False)
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_screener_export_isin_format"),
        CheckConstraint("rows_written > 0", name="ck_screener_export_rows_written"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_screener_export_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_screener_export_no_model"),
        UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_screener_export_version"),
        Index("ix_screener_export_isin_pit", "isin", "consolidation", "as_of"),
    )


class ScreenerValue(ProvenanceMixin, Base):
    """One number from a Screener export's Data Sheet, in absolute units of `unit`.

    `statement` is pl | quarter | bs | cf; `line` is the parser's name for the
    Data Sheet row (ingest/screener_export/parser.py). Blank cells are not
    stored. Rows share their export's content_hash, as_of and rule_version.
    """

    __tablename__ = "screener_value"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    consolidation: Mapped[Consolidation] = mapped_column(_pg_enum(Consolidation, "consolidation"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[str] = mapped_column(Text, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_screener_value_isin_format"),
        CheckConstraint("statement IN ('pl', 'quarter', 'bs', 'cf')", name="ck_screener_value_statement"),
        CheckConstraint("unit IN ('INR', 'shares', 'INR_per_share')", name="ck_screener_value_unit"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date > period_end", name="ck_screener_value_as_of_after_period_end"
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_screener_value_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_screener_value_no_model"),
        UniqueConstraint(
            "content_hash", "as_of", "rule_version", "statement", "line", "period_end", name="uq_screener_value_version"
        ),
        Index("ix_screener_value_pit", "isin", "consolidation", "as_of"),
    )


# --------------------------------------------------------------------------- #
# Store 2: guidance claims from concall transcripts
# --------------------------------------------------------------------------- #


class GuidanceIssueReason(StrEnum):
    """Why a transcript or a claim was held back. Every one is a check by code."""

    # Whole document: nothing from it enters a store
    SHAPE_CHANGED = "shape_changed"
    SCHEMA_REJECTED = "schema_rejected"
    ISIN_UNRESOLVED = "isin_unresolved"
    ISIN_CONFLICT = "isin_conflict"
    IMPLAUSIBLE_AS_OF = "implausible_as_of"
    NO_TEXT_LAYER = "no_text_layer"
    # One claim: the rest of the document is stored
    QUOTE_NOT_IN_TRANSCRIPT = "quote_not_in_transcript"
    QUOTE_AMBIGUOUS = "quote_ambiguous"
    VALUE_NOT_IN_QUOTE = "value_not_in_quote"
    HEDGE_NOT_IN_QUOTE = "hedge_not_in_quote"
    UNMAPPED_HEDGE = "unmapped_hedge"
    UNMAPPED_METRIC = "unmapped_metric"
    VALUE_UNPARSED = "value_unparsed"
    SECTION_MISMATCH = "section_mismatch"
    NOT_FORWARD_LOOKING = "not_forward_looking"


GUIDANCE_ISSUE_REASON = _pg_enum(GuidanceIssueReason, "guidance_issue_reason")
HEDGE_STRENGTH = _pg_enum(HedgeStrength, "hedge_strength")
CLAIM_SECTION = _pg_enum(ClaimSection, "claim_section")
CLAIM_SPECIFICITY = _pg_enum(Specificity, "claim_specificity")
CLAIM_UNIT = _pg_enum(Unit, "claim_unit")


class ConcallDocument(ProvenanceMixin, Base):
    """One transcript file, as text, before any model has read it.

    The ingest half of store 2, and the reason the model half is cheap to
    redo: this row says which company and which call a stored PDF belongs to,
    decided by code from the drop file's name and the dated symbol -> ISIN
    reads (R1). `concall_transcript` rows then point at the same
    (content_hash, as_of) with a model attached.

    `model_version` is NULL here: nothing on this row came from a model.
    """

    __tablename__ = "concall_document"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    isin_basis: Mapped[XbrlIsinBasis] = mapped_column(XBRL_ISIN_BASIS, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    call_date: Mapped[date] = mapped_column(Date, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_concall_document_isin_format"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date >= call_date",
            name="ck_concall_document_as_of_not_before_call",
        ),
        CheckConstraint(
            "char_count > 0 AND page_count >= 0", name="ck_concall_document_counts_non_negative"
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_concall_document_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_concall_document_no_model"),
        UniqueConstraint("content_hash", "as_of", "rule_version", name="uq_concall_document_version"),
        Index("ix_concall_document_isin_pit", "isin", "as_of"),
    )


class ConcallTranscript(ProvenanceMixin, Base):
    """One transcript, as extracted under one model version and one rule version.

    `call_date` is when the call happened; `as_of` is when the transcript was
    published, which is a day or more later. Re-running extraction -- a new
    model version, a corrected prompt, a grown hedge lexicon -- adds a row at
    the document's original `as_of` rather than revising this one, so a replay
    at time t sees whichever extraction was current then.

    Unlike every other filing table here, `model_version` is NOT NULL: these
    rows exist only because a model read the document, and a row that cannot
    name the weights cannot be audited. It names the weights that answered,
    which is not the string the tier was asked for: that is `model_requested`,
    and it is what the loader compares before deciding whether this document
    still has anything new to say.
    """

    __tablename__ = "concall_transcript"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_date: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    claims_written: Mapped[int] = mapped_column(Integer, nullable=False)
    claims_quarantined: Mapped[int] = mapped_column(Integer, nullable=False)
    # The exact prompt that produced this extraction. A prompt edit changes the
    # hash, so two runs that disagree can be told apart from two documents.
    prompt_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    # The tier as asked for ("claude-sonnet-5"), not the snapshot that answered.
    model_requested: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_concall_transcript_isin_format"),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date >= call_date",
            name="ck_concall_transcript_as_of_not_before_call",
        ),
        CheckConstraint(
            "char_count > 0 AND claims_written >= 0 AND claims_quarantined >= 0",
            name="ck_concall_transcript_counts_non_negative",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_concall_transcript_content_hash_sha256"),
        CheckConstraint("prompt_hash ~ '^[0-9a-f]{64}$'", name="ck_concall_transcript_prompt_hash_sha256"),
        CheckConstraint("model_version IS NOT NULL", name="ck_concall_transcript_model_named"),
        UniqueConstraint(
            "content_hash", "as_of", "rule_version", "extractor_version", "model_requested",
            "model_version", "prompt_hash",
            name="uq_concall_transcript_version",
        ),  # fmt: skip
        Index("ix_concall_transcript_isin_pit", "isin", "as_of"),
    )


class GuidanceClaim(ProvenanceMixin, Base):
    """Store 2: one forward-looking commitment, located in its source document.

    The model supplied the verbatim columns (`quote`, `hedge_verbatim`,
    `value_text`, `metric_verbatim`, `period_label`) and the two enumerated
    labels it is allowed to assign (`metric`, `section`). Everything gradable
    -- `hedge_strength`, `specificity`, `value_low`/`value_high`, `value_unit`
    -- was computed from those strings by core/compute/guidance.py under
    `rule_version` (R1).

    `quote_start`/`quote_end` are character offsets into the stored document,
    found by code; the quote is evidence only because it was located in the
    bytes in blob storage.

    Whether guidance was raised, lowered, maintained or withdrawn is NOT a
    column. It is a comparison between two claims, and which two are
    comparable depends on what was known at `t`; storing it would freeze one
    answer and quietly become look-ahead. It is computed at read time
    (core.db.pit.guidance_directions_as_of). Append-only.
    """

    __tablename__ = "guidance_claim"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    call_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    metric_verbatim: Mapped[str] = mapped_column(Text, nullable=False)
    period_label: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_name: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_role: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[ClaimSection] = mapped_column(CLAIM_SECTION, nullable=False)
    hedge_verbatim: Mapped[str] = mapped_column(Text, nullable=False)
    hedge_strength: Mapped[HedgeStrength] = mapped_column(HEDGE_STRENGTH, nullable=False)
    specificity: Mapped[Specificity] = mapped_column(CLAIM_SPECIFICITY, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_low: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    value_high: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    value_unit: Mapped[Unit] = mapped_column(CLAIM_UNIT, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_guidance_claim_isin_format"),
        CheckConstraint("quote_start >= 0 AND quote_end > quote_start", name="ck_guidance_claim_quote_span"),
        CheckConstraint(
            "value_low IS NULL OR value_high IS NULL OR value_low <= value_high",
            name="ck_guidance_claim_value_order",
        ),
        # A row that disagrees with its own specificity is a bug, not data: the
        # parser decides both, so the database refuses the combinations it
        # cannot have produced.
        CheckConstraint(
            "(specificity = 'directional') = (value_low IS NULL AND value_high IS NULL)",
            name="ck_guidance_claim_specificity_matches_value",
        ),
        CheckConstraint(
            "(specificity = 'directional') = (value_text IS NULL)",
            name="ck_guidance_claim_specificity_matches_text",
        ),
        CheckConstraint(
            "specificity <> 'point' OR value_low = value_high",
            name="ck_guidance_claim_point_is_one_number",
        ),
        CheckConstraint(
            "specificity <> 'bound' OR (value_low IS NULL) <> (value_high IS NULL)",
            name="ck_guidance_claim_bound_is_one_sided",
        ),
        CheckConstraint(
            "(timezone('Asia/Kolkata', as_of))::date >= call_date",
            name="ck_guidance_claim_as_of_not_before_call",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_guidance_claim_content_hash_sha256"),
        CheckConstraint("prompt_hash ~ '^[0-9a-f]{64}$'", name="ck_guidance_claim_prompt_hash_sha256"),
        CheckConstraint("model_version IS NOT NULL", name="ck_guidance_claim_model_named"),
        UniqueConstraint(
            "content_hash", "as_of", "rule_version", "extractor_version", "model_version", "prompt_hash",
            "quote_start", "metric", "period_label",
            name="uq_guidance_claim_version",
        ),  # fmt: skip
        Index("ix_guidance_claim_isin_pit", "isin", "metric", "as_of"),
    )


class GuidanceQuarantine(ProvenanceMixin, Base):
    """A transcript or a claim held back for review. Never repaired.

    A whole-document entry has `quote` NULL. Reviewing these is how the hedge
    lexicon and the metric list grow: a claim the model found but code could
    not verify is a finding about the prompt, the document or the lexicon, and
    it is deliberately cheaper to look at than to guess at. Append-only.
    """

    __tablename__ = "guidance_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[GuidanceIssueReason] = mapped_column(GUIDANCE_ISSUE_REASON, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    # NULL when the document was rejected before any model ran: a file with no
    # text layer, or a symbol that resolves to no ISIN.
    extractor_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_guidance_quarantine_content_hash_sha256"),
        CheckConstraint(
            "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
            name="ck_guidance_quarantine_prompt_hash_sha256",
        ),
        Index("ix_guidance_quarantine_pit", "as_of"),
    )


# --- S7b technical screens ---


class TechnicalSignalType(StrEnum):
    VOLUME_SPIKE_UP = "volume_spike_up"
    VOLUME_SPIKE_DOWN = "volume_spike_down"
    CONSOLIDATION_BREAKOUT = "consolidation_breakout"
    CONSOLIDATION_BREAKDOWN = "consolidation_breakdown"
    ATH_BREAKOUT = "ath_breakout"


class TechnicalSignal(ProvenanceMixin, Base):
    """Store ⑩: technical signals computed by code from adjusted daily closes.

    All prices passed in must already be adjusted for corporate actions.
    Severity and type are always computed by code (R1). Append-only,
    enforced by a DB trigger. No model writes to this table.
    `rule_version` tags every parameter set.
    """

    __tablename__ = "technical_signal"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    signal_type: Mapped[TechnicalSignalType] = mapped_column(
        _pg_enum(TechnicalSignalType, "technical_signal_type"), nullable=False
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    volume_median_50d: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    price_return_1d: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    consolidation_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    consolidation_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    consolidation_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    ath_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_technical_signal_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_technical_signal_no_model"),
        CheckConstraint("close_price > 0", name="ck_technical_signal_close_price_positive"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_technical_signal_content_hash_sha256"
        ),
        UniqueConstraint(
            "isin", "signal_type", "signal_date", "rule_version",
            name="uq_technical_signal_version",
        ),
        Index("ix_technical_signal_isin_date", "isin", "signal_date"),
    )


class WatchlistEntryStatus(StrEnum):
    OPEN = "open"
    PROMOTED = "promoted"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class WatchlistEntry(ProvenanceMixin, Base):
    """Store ⑪: watchlist entries opened by technical signals.

    A technical signal only opens an entry; promotion to ADD_REVIEW requires
    the ADD_REVIEW gates (CLAUDE.md Decision support). No model writes to
    this table. Append-only, enforced by a DB trigger.
    """

    __tablename__ = "watchlist_entry"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    status: Mapped[WatchlistEntryStatus] = mapped_column(
        _pg_enum(WatchlistEntryStatus, "watchlist_entry_status"), nullable=False
    )
    opened_by_signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("technical_signal.id"), nullable=False
    )
    opened_date: Mapped[date] = mapped_column(Date, nullable=False)
    status_date: Mapped[date] = mapped_column(Date, nullable=False)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_watchlist_entry_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_watchlist_entry_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_watchlist_entry_content_hash_sha256"
        ),
        Index("ix_watchlist_entry_isin_status", "isin", "status"),
        Index("ix_watchlist_entry_status_date", "status", "status_date"),
    )


# --- Credit rating actions (session 8) ---


class RatingAction(ProvenanceMixin, Base):
    """Store ⑧: Credit rating actions (CRISIL/ICRA/CARE/India Ratings/Brickwork/Acuite).

    `action_date` is the date the agency published the rating action.
    `raw_rating` is the verbatim string from the agency; `common_scale` is the
    normalised equivalent. `severity` and direction flags are computed by code,
    never by a model (R1). `evidence_url` is required on every row (CLAUDE.md).
    Append-only.
    """

    __tablename__ = "rating_action"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    agency: Mapped[RatingAgency] = mapped_column(
        _pg_enum(RatingAgency, "rating_agency"), nullable=False
    )
    instrument_type: Mapped[str] = mapped_column(Text, nullable=False)
    raw_rating: Mapped[str] = mapped_column(Text, nullable=False)
    common_scale: Mapped[CommonRatingScale] = mapped_column(
        _pg_enum(CommonRatingScale, "common_rating_scale"), nullable=False
    )
    outlook: Mapped[RatingOutlook | None] = mapped_column(
        _pg_enum(RatingOutlook, "rating_outlook"), nullable=True
    )
    action_date: Mapped[date] = mapped_column(Date, nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    is_upgrade: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_downgrade: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_withdrawn: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evidence_url: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_rating_action_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_rating_action_no_model"),
        CheckConstraint(
            "evidence_url IS NOT NULL AND evidence_url != ''",
            name="ck_rating_action_evidence_url_required",
        ),
        CheckConstraint(
            "severity BETWEEN 1 AND 5", name="ck_rating_action_severity_range"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_rating_action_content_hash_sha256"
        ),
        Index("ix_rating_action_isin_date", "isin", "action_date"),
    )


# --- S7 company_event ---


class CompanyEventSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CompanyEventType(StrEnum):
    CWIP_TO_GROSS_BLOCK = "cwip_to_gross_block"
    REVENUE_STEP_UP = "revenue_step_up"
    MARGIN_COMPRESSION = "margin_compression"
    DEBT_SPIKE = "debt_spike"
    PROMOTER_PLEDGE = "promoter_pledge"
    PROMOTER_PLEDGE_INVOKED = "promoter_pledge_invoked"
    RATING_DOWNGRADE = "rating_downgrade"
    RATING_WITHDRAWAL = "rating_withdrawal"
    RAID_REGULATORY = "raid_regulatory"


class CompanyEvent(ProvenanceMixin, Base):
    """Store : Red flags and commissioning signals, computed by code.

    Severity is always computed by code, never asserted by a model (R1).
    Every row requires an evidence_url (CLAUDE.md). Append-only.
    """

    __tablename__ = "company_event"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    event_type: Mapped[CompanyEventType] = mapped_column(
        _pg_enum(CompanyEventType, "company_event_type"), nullable=False
    )
    severity: Mapped[CompanyEventSeverity] = mapped_column(
        _pg_enum(CompanyEventSeverity, "company_event_severity"), nullable=False
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str | None] = mapped_column(Text, nullable=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    evidence_url: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_company_event_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_company_event_no_model"),
        CheckConstraint(
            "evidence_url IS NOT NULL AND evidence_url != ''",
            name="ck_company_event_evidence_url_required",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_company_event_content_hash_sha256"
        ),
        Index("ix_company_event_isin_date", "isin", "event_date"),
        Index("ix_company_event_type_date", "event_type", "event_date"),
    )


# --- Ownership events and catalysts (session 7c) ---


class AcquisitionMode(StrEnum):
    OPEN_MARKET = "open_market"
    PREFERENTIAL_ALLOTMENT = "preferential_allotment"
    ESOS_ESOP = "esos_esop"
    OFF_MARKET = "off_market"
    GIFT = "gift"
    INHERITANCE = "inheritance"
    PLEDGE_INVOCATION = "pledge_invocation"
    OTHER = "other"


ACQUISITION_MODE = _pg_enum(AcquisitionMode, "acquisition_mode")


class InsiderTrade(ProvenanceMixin, Base):
    """Promoter / insider trade disclosure as filed with NSE/BSE.

    Each row is one trade event. Append-only; a correction is a new row
    with a later `as_of`.
    """

    __tablename__ = "insider_trade"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    person_name: Mapped[str] = mapped_column(Text, nullable=False)
    person_category: Mapped[str] = mapped_column(Text, nullable=False)
    acquisition_mode: Mapped[AcquisitionMode] = mapped_column(ACQUISITION_MODE, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 0), nullable=False)
    price_per_share: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    post_trade_holding_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_insider_trade_isin_format"),
        CheckConstraint("model_version IS NULL", name="ck_insider_trade_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_insider_trade_content_hash_sha256"
        ),
        Index("ix_insider_trade_isin_date", "isin", "trade_date"),
    )


class DisclosureType(StrEnum):
    SAST_5PCT = "sast_5pct"
    SAST_10PCT = "sast_10pct"
    SAST_25PCT = "sast_25pct"
    SAST_CREEPING = "sast_creeping"
    PLEDGE_CREATED = "pledge_created"
    PLEDGE_RELEASED = "pledge_released"
    PLEDGE_INVOKED = "pledge_invoked"
    RECLASSIFICATION = "reclassification"


DISCLOSURE_TYPE = _pg_enum(DisclosureType, "disclosure_type")


class StakeDisclosure(ProvenanceMixin, Base):
    """Large-stake crossing or pledge event disclosure (SAST / pledge regulations).

    Append-only; each regulatory trigger is a separate row.
    """

    __tablename__ = "stake_disclosure"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    acquirer_name: Mapped[str] = mapped_column(Text, nullable=False)
    disclosure_type: Mapped[DisclosureType] = mapped_column(DISCLOSURE_TYPE, nullable=False)
    disclosure_date: Mapped[date] = mapped_column(Date, nullable=False)
    shares_acquired: Mapped[Decimal | None] = mapped_column(Numeric(28, 0), nullable=True)
    post_acquisition_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_stake_disclosure_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_stake_disclosure_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_stake_disclosure_content_hash_sha256"
        ),
        Index("ix_stake_disclosure_isin_date", "isin", "disclosure_date"),
    )


class DealType(StrEnum):
    BULK = "bulk"
    BLOCK = "block"


DEAL_TYPE = _pg_enum(DealType, "deal_type")


class DealSide(StrEnum):
    INFLOW = "inflow"  # Cash inflow: client acquiring shares
    OUTFLOW = "outflow"  # Cash outflow: client selling shares


DEAL_SIDE = _pg_enum(DealSide, "deal_side")


class BulkBlockDeal(ProvenanceMixin, Base):
    """Bulk or block deal record as published by NSE/BSE.

    Append-only; each deal is a separate row.
    """

    __tablename__ = "bulk_block_deal"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    deal_type: Mapped[DealType] = mapped_column(DEAL_TYPE, nullable=False)
    deal_date: Mapped[date] = mapped_column(Date, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 0), nullable=False)
    price_per_share: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    deal_side: Mapped[DealSide] = mapped_column(DEAL_SIDE, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_bulk_block_deal_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_bulk_block_deal_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_bulk_block_deal_content_hash_sha256"
        ),
        Index("ix_bulk_block_deal_isin_date", "isin", "deal_date"),
    )


class ScheduledEventType(StrEnum):
    BOARD_MEETING = "board_meeting"
    RESULTS = "results"
    AGM = "agm"
    EGM = "egm"
    RECORD_DATE = "record_date"
    EX_DIVIDEND = "ex_dividend"
    RIGHTS_RECORD = "rights_record"
    BUYBACK_OPEN = "buyback_open"
    BUYBACK_CLOSE = "buyback_close"
    OFS_OPEN = "ofs_open"
    OFS_CLOSE = "ofs_close"
    # Demerger milestone chain (a record date reuses RECORD_DATE)
    DEMERGER_SCHEME = "demerger_scheme"
    DEMERGER_BOARD = "demerger_board"
    DEMERGER_SHAREHOLDER = "demerger_shareholder"
    DEMERGER_CREDITOR = "demerger_creditor"
    DEMERGER_NCLT_ORDER = "demerger_nclt_order"
    DEMERGER_LISTING = "demerger_listing"


SCHEDULED_EVENT_TYPE = _pg_enum(ScheduledEventType, "scheduled_event_type")


class ScheduledEventSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SCHEDULED_EVENT_SEVERITY = _pg_enum(ScheduledEventSeverity, "scheduled_event_severity")


class ScheduledEvent(ProvenanceMixin, Base):
    """Catalyst calendar entry from exchange announcements.

    Severity is computed by code (R1). Append-only; an outcome is a new row
    with the same isin+event_type+event_date and a later `as_of`.
    """

    __tablename__ = "scheduled_event"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    event_type: Mapped[ScheduledEventType] = mapped_column(SCHEDULED_EVENT_TYPE, nullable=False)
    severity: Mapped[ScheduledEventSeverity] = mapped_column(
        SCHEDULED_EVENT_SEVERITY, nullable=False
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_scheduled_event_isin_format"
        ),
        CheckConstraint("model_version IS NULL", name="ck_scheduled_event_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_scheduled_event_content_hash_sha256"
        ),
        Index("ix_scheduled_event_isin_date", "isin", "event_date"),
        Index("ix_scheduled_event_type_date", "event_type", "event_date"),
    )


class IndexEventType(StrEnum):
    INCLUSION = "inclusion"
    EXCLUSION = "exclusion"
    WEIGHT_CHANGE = "weight_change"
    FO_INCLUSION = "fo_inclusion"
    FO_EXCLUSION = "fo_exclusion"


INDEX_EVENT_TYPE = _pg_enum(IndexEventType, "index_event_type")


class IndexEventStatus(StrEnum):
    ANNOUNCED = "announced"
    EFFECTIVE = "effective"
    REVISED = "revised"
    CANCELLED = "cancelled"


INDEX_EVENT_STATUS = _pg_enum(IndexEventStatus, "index_event_status")


class IndexEvent(ProvenanceMixin, Base):
    """Index review event (inclusion / exclusion / weight change / F&O eligibility).

    Lifecycle: ANNOUNCED -> EFFECTIVE | REVISED | CANCELLED. Each state
    transition is a new row with a later `as_of`. Append-only.
    """

    __tablename__ = "index_event"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    index_code: Mapped[IndexCode] = mapped_column(INDEX_CODE, nullable=False)
    event_type: Mapped[IndexEventType] = mapped_column(INDEX_EVENT_TYPE, nullable=False)
    status: Mapped[IndexEventStatus] = mapped_column(INDEX_EVENT_STATUS, nullable=False)
    announced_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    old_weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    new_weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_index_event_isin_format"),
        CheckConstraint("model_version IS NULL", name="ck_index_event_no_model"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_index_event_content_hash_sha256"
        ),
        Index("ix_index_event_isin", "isin"),
        Index("ix_index_event_code_status", "index_code", "status"),
    )


# --- Order wins (session 7e) ---


class OrderWinQuarantineReason(StrEnum):
    """Why an announcement that looked like an order was held back.

    `core.compute.order_wins.QuarantineReason` minus NOT_AN_ORDER (most
    announcements are not orders; they are counted, never stored), plus the
    one reason only the adapter can see.
    """

    PRE_AWARD = "pre_award"
    EXCLUDED = "excluded"
    AMBIGUOUS = "ambiguous"
    NO_VALUE = "no_value"
    MULTIPLE_VALUES = "multiple_values"
    INVALID_ISIN = "invalid_isin"


ORDER_WIN_QUARANTINE_REASON = _pg_enum(OrderWinQuarantineReason, "order_win_quarantine_reason")


class OrderWinFact(ProvenanceMixin, Base):
    """An order win read from one NSE announcement, every field traceable to its quote.

    `announcement_key` is the sha256 of the announcement's own text and time, so
    the same announcement seen in two overlapping listings is one fact.
    `content_hash` is the raw listing file it came from (`raw_source_file`);
    `as_of` is the announcement's dissemination time. A new `rule_version` is a
    new version of the same announcement. Gaps are named in `missing`, never
    filled. Append-only; read through core.db.pit.order_wins_as_of.
    """

    __tablename__ = "order_win"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    announcement_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    announced_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_value_inr: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    value_quote: Mapped[str] = mapped_column(Text, nullable=False)
    counterparty: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_months: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    execution_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_phrase: Mapped[str] = mapped_column(Text, nullable=False)
    missing: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_order_win_isin_format"),
        CheckConstraint(
            "announcement_key ~ '^[0-9a-f]{64}$'", name="ck_order_win_announcement_key_sha256"
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_order_win_content_hash_sha256"),
        CheckConstraint("model_version IS NULL", name="ck_order_win_no_model"),
        CheckConstraint("order_value_inr > 0", name="ck_order_win_value_positive"),
        CheckConstraint(
            "(counterparty IS NULL) = (counterparty_quote IS NULL)",
            name="ck_order_win_counterparty_with_quote",
        ),
        CheckConstraint(
            "(execution_months IS NULL) = (period_quote IS NULL)",
            name="ck_order_win_period_with_quote",
        ),
        CheckConstraint(
            "execution_end IS NULL OR execution_months IS NOT NULL",
            name="ck_order_win_end_needs_period",
        ),
        CheckConstraint(
            "(counterparty IS NULL) = ('counterparty' = ANY(missing))"
            " AND (execution_months IS NULL) = ('execution_period' = ANY(missing))",
            name="ck_order_win_missing_names_the_gaps",
        ),
        UniqueConstraint("announcement_key", "rule_version", name="uq_order_win_announcement_rule"),
        Index("ix_order_win_isin_announced", "isin", "announced_on"),
    )


class OrderWinQuarantine(ProvenanceMixin, Base):
    """An order-like announcement held back for review, with the reason and the text that decided it.

    `isin` is NULL only when the listing's ISIN was malformed (the raw value is
    then in `quote`). Append-only.
    """

    __tablename__ = "order_win_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    isin: Mapped[str | None] = mapped_column(CHAR(12), nullable=True)
    announcement_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    announced_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[OrderWinQuarantineReason] = mapped_column(
        ORDER_WIN_QUARANTINE_REASON, nullable=False
    )
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "isin IS NULL OR isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_order_win_quarantine_isin_format"
        ),
        CheckConstraint(
            "announcement_key ~ '^[0-9a-f]{64}$'",
            name="ck_order_win_quarantine_announcement_key_sha256",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_order_win_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_order_win_quarantine_no_model"),
        UniqueConstraint(
            "announcement_key", "rule_version", name="uq_order_win_quarantine_announcement_rule"
        ),
        Index("ix_order_win_quarantine_pit", "as_of"),
    )


# --- Broker holdings drop (read-only) ---


class BrokerHoldingQuarantineReason(StrEnum):
    """Why a row of a broker holdings file was held back instead of stored."""

    MISSING_ISIN = "missing_isin"
    INVALID_ISIN = "invalid_isin"
    NEGATIVE_QUANTITY = "negative_quantity"
    UNPARSEABLE_NUMBER = "unparseable_number"
    UNKNOWN_EXCHANGE = "unknown_exchange"
    DUPLICATE_ROW = "duplicate_row"


BROKER_HOLDING_QUARANTINE_REASON = _pg_enum(
    BrokerHoldingQuarantineReason, "broker_holding_quarantine_reason"
)


class BrokerHoldingSnapshot(ProvenanceMixin, Base):
    """One position of one account in one dropped broker holdings file, keyed by ISIN.

    `snapshot_at` is when the broker reported the position; `as_of` is when this
    system could first know it (the sidecar's `published_at`), never earlier.
    `content_hash` is the dropped file in `raw_source_file`. The same company
    may appear once per exchange, so one ISIN can have an NSE and a BSE row in
    the same snapshot. No trading symbol is stored: a series suffix such as
    "-BE" is not a different security. Append-only; read through
    core.db.pit.holdings_as_of.
    """

    __tablename__ = "broker_holding_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_label: Mapped[str] = mapped_column(Text, nullable=False)
    isin: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    last_price: Mapped[Decimal] = mapped_column(Numeric(28, 6), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("isin ~ '^IN[A-Z0-9]{9}[0-9]$'", name="ck_broker_holding_snapshot_isin_format"),
        CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_broker_holding_snapshot_exchange"),
        CheckConstraint("quantity >= 0", name="ck_broker_holding_snapshot_quantity"),
        CheckConstraint("average_price >= 0", name="ck_broker_holding_snapshot_average_price"),
        CheckConstraint("last_price >= 0", name="ck_broker_holding_snapshot_last_price"),
        CheckConstraint("account_label <> ''", name="ck_broker_holding_snapshot_account"),
        CheckConstraint("as_of >= snapshot_at", name="ck_broker_holding_snapshot_known_after_taken"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_broker_holding_snapshot_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_broker_holding_snapshot_no_model"),
        UniqueConstraint(
            "account_label",
            "isin",
            "exchange",
            "snapshot_at",
            "content_hash",
            name="uq_broker_holding_snapshot_key",
        ),
        Index("ix_broker_holding_snapshot_pit", "account_label", "isin", "snapshot_at"),
    )


class BrokerHoldingQuarantine(ProvenanceMixin, Base):
    """A row of a broker holdings file held back for review, with the reason.

    `isin` is the raw value from the file (absent or malformed for two of the
    reasons). `row_number` counts from 1 in the file's list. Append-only.
    """

    __tablename__ = "broker_holding_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_label: Mapped[str] = mapped_column(Text, nullable=False)
    isin: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[BrokerHoldingQuarantineReason] = mapped_column(
        BROKER_HOLDING_QUARANTINE_REASON, nullable=False
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("row_number >= 1", name="ck_broker_holding_quarantine_row_number"),
        CheckConstraint("as_of >= snapshot_at", name="ck_broker_holding_quarantine_known_after_taken"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_broker_holding_quarantine_content_hash_sha256"
        ),
        CheckConstraint("model_version IS NULL", name="ck_broker_holding_quarantine_no_model"),
        UniqueConstraint("content_hash", "row_number", name="uq_broker_holding_quarantine_row"),
        Index("ix_broker_holding_quarantine_pit", "as_of"),
    )
