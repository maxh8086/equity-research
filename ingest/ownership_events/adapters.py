"""Ownership-event drop-folder adapters (CLAUDE.md "Data sources": manual_drop).

Five adapters, each reading a separate CSV from the drop folder:
  - InsiderTradeDrop        → insider_trade
  - StakeDisclosureDrop     → stake_disclosure
  - BulkBlockDealDrop       → bulk_block_deal
  - ScheduledEventDrop      → scheduled_event
  - IndexEventDrop          → index_event

No LLM is involved. All fields come from structured CSV columns.
model_version is never set on any row produced here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from core.db.models import (
    AcquisitionMode,
    BulkBlockDeal,
    DealSide,
    DealType,
    DisclosureType,
    IndexCode,
    IndexEvent,
    IndexEventStatus,
    IndexEventType,
    InsiderTrade,
    ScheduledEvent,
    ScheduledEventSeverity,
    ScheduledEventType,
    StakeDisclosure,
)
from core.sources import SourceClass
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.schema import StrictModel

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

RULE_VERSION = "ownership_events_drop_v1"


def _provenance(adapter: Adapter, doc: RawDocument) -> dict:
    return dict(
        as_of=doc.as_of,
        content_hash=doc.content_hash,
        source_url=doc.source_url,
        extracted_by=adapter.extracted_by(),
        model_version=None,
    )


def _parse_date(s: str, field_name: str) -> date:
    s = s.strip()
    if not s:
        raise ValueError(f"{field_name}: date is required but was empty")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{field_name}: cannot parse date {s!r} (expected YYYY-MM-DD)")


def _parse_date_optional(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"cannot parse date {s!r} (expected YYYY-MM-DD)")


def _parse_decimal(s: str, field_name: str) -> Decimal:
    s = s.strip().replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{field_name}: cannot parse decimal {s!r}")


def _parse_decimal_optional(s: str) -> Decimal | None:
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"cannot parse decimal {s!r}")


class _ParseError(Exception):
    pass


@dataclass
class Tally:
    raw_files: int = 0
    rows_written: int = 0
    rejected_files: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter_name: str) -> RunResult:
        detail = f"{self.rows_written} rows written"
        if self.rejected_files:
            detail = f"{self.rejected_files} files rejected, {self.rows_written} rows written"
        if self.skipped:
            detail += f", {self.skipped} already loaded"
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        failed = bool(self.rejected_files or self.problems)
        return RunResult(
            adapter_name,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.rows_written,
        )


def _load_csv(doc: RawDocument, required_columns: frozenset[str], row_cls: type) -> list:
    """Decode and parse a drop CSV; return list of validated row model objects."""
    text = doc.data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise _ParseError("CSV file is empty or has no header")
    missing = required_columns - set(reader.fieldnames)
    if missing:
        raise _ParseError(f"CSV missing required columns: {sorted(missing)}")
    rows = []
    for i, raw in enumerate(reader, start=2):
        known = {k: v for k, v in raw.items() if k in row_cls.model_fields}
        rows.append((i, row_cls.model_validate(known, strict=False)))
    return rows


# --------------------------------------------------------------------------- #
# InsiderTrade adapter
# --------------------------------------------------------------------------- #

TARGET_STORES_INSIDER = ("insider_trade",)

REQUIRED_COLUMNS_INSIDER = frozenset({
    "isin",
    "person_name",
    "person_category",
    "acquisition_mode",
    "trade_date",
    "quantity",
    "price_per_share",
})


class InsiderTradeRow(StrictModel):
    isin: str
    person_name: str
    person_category: str
    acquisition_mode: str
    trade_date: str
    quantity: str
    price_per_share: str
    post_trade_holding_pct: str = ""


def _parse_insider_row(validated: InsiderTradeRow, row_number: int) -> dict:
    try:
        return dict(
            isin=validated.isin.strip(),
            person_name=validated.person_name.strip(),
            person_category=validated.person_category.strip(),
            acquisition_mode=AcquisitionMode(validated.acquisition_mode.strip()),
            trade_date=_parse_date(validated.trade_date, "trade_date"),
            quantity=_parse_decimal(validated.quantity, "quantity"),
            price_per_share=_parse_decimal(validated.price_per_share, "price_per_share"),
            post_trade_holding_pct=_parse_decimal_optional(validated.post_trade_holding_pct),
        )
    except (ValueError, KeyError) as exc:
        raise _ParseError(f"row {row_number}: {exc}") from exc


def _load_insider_trades(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    try:
        parsed_rows = _load_csv(doc, REQUIRED_COLUMNS_INSIDER, InsiderTradeRow)
        rows = []
        for row_number, validated in parsed_rows:
            kwargs = _parse_insider_row(validated, row_number)
            rows.append(InsiderTrade(**kwargs, **prov))
    except _ParseError as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url}: {exc}")
        return
    ctx.session.add_all(rows)
    tally.rows_written += len(rows)


class InsiderTradeDrop(DropFolderAdapter):
    """Insider trade disclosures from NSE/BSE downloaded by hand into
    <EQUITY_DROP_FOLDER>/insider_trade_drop/.
    """

    name = "insider_trade_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = TARGET_STORES_INSIDER

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            _load_insider_trades(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            text = path.read_bytes().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames is None:
                raise RuntimeError(f"{path.name}: CSV file is empty or has no header")
            missing = REQUIRED_COLUMNS_INSIDER - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f"{path.name}: missing required columns: {sorted(missing)}")


# --------------------------------------------------------------------------- #
# StakeDisclosure adapter
# --------------------------------------------------------------------------- #

TARGET_STORES_STAKE = ("stake_disclosure",)

REQUIRED_COLUMNS_STAKE = frozenset({
    "isin",
    "acquirer_name",
    "disclosure_type",
    "disclosure_date",
})


class StakeDisclosureRow(StrictModel):
    isin: str
    acquirer_name: str
    disclosure_type: str
    disclosure_date: str
    shares_acquired: str = ""
    post_acquisition_pct: str = ""


def _parse_stake_row(validated: StakeDisclosureRow, row_number: int) -> dict:
    try:
        return dict(
            isin=validated.isin.strip(),
            acquirer_name=validated.acquirer_name.strip(),
            disclosure_type=DisclosureType(validated.disclosure_type.strip()),
            disclosure_date=_parse_date(validated.disclosure_date, "disclosure_date"),
            shares_acquired=_parse_decimal_optional(validated.shares_acquired),
            post_acquisition_pct=_parse_decimal_optional(validated.post_acquisition_pct),
        )
    except (ValueError, KeyError) as exc:
        raise _ParseError(f"row {row_number}: {exc}") from exc


def _load_stake_disclosures(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    try:
        parsed_rows = _load_csv(doc, REQUIRED_COLUMNS_STAKE, StakeDisclosureRow)
        rows = []
        for row_number, validated in parsed_rows:
            kwargs = _parse_stake_row(validated, row_number)
            rows.append(StakeDisclosure(**kwargs, **prov))
    except _ParseError as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url}: {exc}")
        return
    ctx.session.add_all(rows)
    tally.rows_written += len(rows)


class StakeDisclosureDrop(DropFolderAdapter):
    """Stake disclosure filings (SAST, pledges, reclassification) downloaded by
    hand into <EQUITY_DROP_FOLDER>/stake_disclosure_drop/.
    """

    name = "stake_disclosure_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = TARGET_STORES_STAKE

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            _load_stake_disclosures(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            text = path.read_bytes().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames is None:
                raise RuntimeError(f"{path.name}: CSV file is empty or has no header")
            missing = REQUIRED_COLUMNS_STAKE - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f"{path.name}: missing required columns: {sorted(missing)}")


# --------------------------------------------------------------------------- #
# BulkBlockDeal adapter
# --------------------------------------------------------------------------- #

TARGET_STORES_DEAL = ("bulk_block_deal",)

REQUIRED_COLUMNS_DEAL = frozenset({
    "isin",
    "deal_type",
    "deal_date",
    "client_name",
    "quantity",
    "price_per_share",
    "deal_side",
})


class BulkBlockDealRow(StrictModel):
    isin: str
    deal_type: str
    deal_date: str
    client_name: str
    quantity: str
    price_per_share: str
    deal_side: str


def _parse_deal_row(validated: BulkBlockDealRow, row_number: int) -> dict:
    try:
        return dict(
            isin=validated.isin.strip(),
            deal_type=DealType(validated.deal_type.strip()),
            deal_date=_parse_date(validated.deal_date, "deal_date"),
            client_name=validated.client_name.strip(),
            quantity=_parse_decimal(validated.quantity, "quantity"),
            price_per_share=_parse_decimal(validated.price_per_share, "price_per_share"),
            deal_side=DealSide(validated.deal_side.strip()),
        )
    except (ValueError, KeyError) as exc:
        raise _ParseError(f"row {row_number}: {exc}") from exc


def _load_bulk_block_deals(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    try:
        parsed_rows = _load_csv(doc, REQUIRED_COLUMNS_DEAL, BulkBlockDealRow)
        rows = []
        for row_number, validated in parsed_rows:
            kwargs = _parse_deal_row(validated, row_number)
            rows.append(BulkBlockDeal(**kwargs, **prov))
    except _ParseError as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url}: {exc}")
        return
    ctx.session.add_all(rows)
    tally.rows_written += len(rows)


class BulkBlockDealDrop(DropFolderAdapter):
    """Bulk and block deal data downloaded by hand into
    <EQUITY_DROP_FOLDER>/bulk_block_deal_drop/.
    """

    name = "bulk_block_deal_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = TARGET_STORES_DEAL

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            _load_bulk_block_deals(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            text = path.read_bytes().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames is None:
                raise RuntimeError(f"{path.name}: CSV file is empty or has no header")
            missing = REQUIRED_COLUMNS_DEAL - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f"{path.name}: missing required columns: {sorted(missing)}")


# --------------------------------------------------------------------------- #
# ScheduledEvent adapter
# --------------------------------------------------------------------------- #

TARGET_STORES_SCHED = ("scheduled_event",)

REQUIRED_COLUMNS_SCHED = frozenset({
    "isin",
    "event_type",
    "severity",
    "event_date",
})


class ScheduledEventRow(StrictModel):
    isin: str
    event_type: str
    severity: str
    event_date: str
    description: str = ""
    outcome: str = ""


def _parse_sched_row(validated: ScheduledEventRow, row_number: int) -> dict:
    try:
        return dict(
            isin=validated.isin.strip(),
            event_type=ScheduledEventType(validated.event_type.strip()),
            severity=ScheduledEventSeverity(validated.severity.strip()),
            event_date=_parse_date(validated.event_date, "event_date"),
            description=validated.description.strip() or None,
            outcome=validated.outcome.strip() or None,
        )
    except (ValueError, KeyError) as exc:
        raise _ParseError(f"row {row_number}: {exc}") from exc


def _load_scheduled_events(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    try:
        parsed_rows = _load_csv(doc, REQUIRED_COLUMNS_SCHED, ScheduledEventRow)
        rows = []
        for row_number, validated in parsed_rows:
            kwargs = _parse_sched_row(validated, row_number)
            rows.append(ScheduledEvent(**kwargs, **prov))
    except _ParseError as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url}: {exc}")
        return
    ctx.session.add_all(rows)
    tally.rows_written += len(rows)


class ScheduledEventDrop(DropFolderAdapter):
    """Catalyst calendar events downloaded by hand into
    <EQUITY_DROP_FOLDER>/scheduled_event_drop/.
    """

    name = "scheduled_event_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = TARGET_STORES_SCHED

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            _load_scheduled_events(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            text = path.read_bytes().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames is None:
                raise RuntimeError(f"{path.name}: CSV file is empty or has no header")
            missing = REQUIRED_COLUMNS_SCHED - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f"{path.name}: missing required columns: {sorted(missing)}")


# --------------------------------------------------------------------------- #
# IndexEvent adapter
# --------------------------------------------------------------------------- #

TARGET_STORES_INDEX = ("index_event",)

REQUIRED_COLUMNS_INDEX = frozenset({
    "isin",
    "index_code",
    "event_type",
    "status",
    "announced_date",
})


class IndexEventRow(StrictModel):
    isin: str
    index_code: str
    event_type: str
    status: str
    announced_date: str
    effective_date: str = ""
    old_weight: str = ""
    new_weight: str = ""


def _parse_index_event_row(validated: IndexEventRow, row_number: int) -> dict:
    try:
        return dict(
            isin=validated.isin.strip(),
            index_code=IndexCode(validated.index_code.strip()),
            event_type=IndexEventType(validated.event_type.strip()),
            status=IndexEventStatus(validated.status.strip()),
            announced_date=_parse_date(validated.announced_date, "announced_date"),
            effective_date=_parse_date_optional(validated.effective_date),
            old_weight=_parse_decimal_optional(validated.old_weight),
            new_weight=_parse_decimal_optional(validated.new_weight),
        )
    except (ValueError, KeyError) as exc:
        raise _ParseError(f"row {row_number}: {exc}") from exc


def _load_index_events(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    try:
        parsed_rows = _load_csv(doc, REQUIRED_COLUMNS_INDEX, IndexEventRow)
        rows = []
        for row_number, validated in parsed_rows:
            kwargs = _parse_index_event_row(validated, row_number)
            rows.append(IndexEvent(**kwargs, **prov))
    except _ParseError as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url}: {exc}")
        return
    ctx.session.add_all(rows)
    tally.rows_written += len(rows)


class IndexEventDrop(DropFolderAdapter):
    """Index inclusion/exclusion events downloaded by hand into
    <EQUITY_DROP_FOLDER>/index_event_drop/.
    """

    name = "index_event_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = TARGET_STORES_INDEX

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            _load_index_events(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            text = path.read_bytes().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames is None:
                raise RuntimeError(f"{path.name}: CSV file is empty or has no header")
            missing = REQUIRED_COLUMNS_INDEX - set(reader.fieldnames)
            if missing:
                raise RuntimeError(f"{path.name}: missing required columns: {sorted(missing)}")
