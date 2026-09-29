"""Tests for the ownership-event drop-folder adapters.

These tests exercise the CSV parsing logic in
`ingest/ownership_events/adapters.py` using fixture files in
`tests/fixtures/ownership_events/`.

The tests use a lightweight in-memory SQLite approach via mocks so they
run without a live Postgres instance.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "ownership_events"

IST = timezone.utc  # Use UTC as a stand-in for IST in unit tests

AS_OF = datetime(2024, 9, 1, 0, 0, tzinfo=IST)
CONTENT_HASH = "a" * 64
SOURCE_URL = "file:///drop/test.csv"


def _make_doc(path: Path) -> MagicMock:
    """Build a minimal RawDocument-like mock from a fixture CSV path."""
    doc = MagicMock()
    doc.as_of = AS_OF
    doc.content_hash = CONTENT_HASH
    doc.source_url = SOURCE_URL
    doc.data = path.read_bytes()
    return doc


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.extracted_by.return_value = "test_adapter/v1"
    return adapter


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.session = MagicMock()
    return ctx


# --------------------------------------------------------------------------- #
# Import the load functions directly so we can unit-test them without the
# full adapter lifecycle.
# --------------------------------------------------------------------------- #

from ingest.ownership_events.adapters import (
    Tally,
    _load_bulk_block_deals,
    _load_index_events,
    _load_insider_trades,
    _load_scheduled_events,
    _load_stake_disclosures,
)


class TestInsiderTradeAdapter:
    def test_happy_path(self):
        doc = _make_doc(FIXTURES / "insider_trade.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_insider_trades(adapter, ctx, doc, tally)

        assert tally.raw_files == 1
        assert tally.rows_written == 2
        assert tally.rejected_files == 0
        assert tally.problems == []
        assert ctx.session.add_all.call_count == 1
        rows = ctx.session.add_all.call_args[0][0]
        assert len(rows) == 2
        r0 = rows[0]
        assert r0.isin == "INE848E01016"
        assert r0.person_name == "Ratan Tata"
        assert r0.trade_date == date(2024, 3, 15)
        assert r0.quantity == Decimal("50000")
        assert r0.price_per_share == Decimal("850.50")
        assert r0.post_trade_holding_pct == Decimal("42.31")
        assert r0.model_version is None
        r1 = rows[1]
        assert r1.post_trade_holding_pct is None

    def test_missing_column_rejected(self):
        bad_csv = b"isin,person_name,acquisition_mode,trade_date\nINE848E01016,X,open_market,2024-01-01\n"
        doc = _make_doc.__wrapped__(bad_csv) if hasattr(_make_doc, "__wrapped__") else _make_doc(FIXTURES / "insider_trade.csv")
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_insider_trades(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1
        assert tally.rows_written == 0
        assert len(tally.problems) == 1

    def test_bad_acquisition_mode_rejected(self):
        bad_csv = (
            b"isin,person_name,person_category,acquisition_mode,trade_date,quantity,price_per_share\n"
            b"INE848E01016,Test Person,Promoter,invalid_mode,2024-01-01,100,500.00\n"
        )
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_insider_trades(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1

    def test_bad_date_rejected(self):
        bad_csv = (
            b"isin,person_name,person_category,acquisition_mode,trade_date,quantity,price_per_share\n"
            b"INE848E01016,Test Person,Promoter,open_market,not-a-date,100,500.00\n"
        )
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_insider_trades(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1


class TestStakeDisclosureAdapter:
    def test_happy_path(self):
        doc = _make_doc(FIXTURES / "stake_disclosure.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_stake_disclosures(adapter, ctx, doc, tally)

        assert tally.raw_files == 1
        assert tally.rows_written == 2
        assert tally.rejected_files == 0
        rows = ctx.session.add_all.call_args[0][0]
        r0 = rows[0]
        assert r0.isin == "INE848E01016"
        assert r0.acquirer_name == "Tata Sons Pvt Ltd"
        assert r0.disclosure_date == date(2024, 4, 10)
        assert r0.shares_acquired == Decimal("5000000")
        assert r0.post_acquisition_pct == Decimal("46.25")
        assert r0.model_version is None
        r1 = rows[1]
        assert r1.shares_acquired is None
        assert r1.post_acquisition_pct is None

    def test_missing_required_column_rejected(self):
        bad_csv = b"isin,acquirer_name,disclosure_date\nINE848E01016,X,2024-01-01\n"
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_stake_disclosures(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1


class TestBulkBlockDealAdapter:
    def test_happy_path(self):
        doc = _make_doc(FIXTURES / "bulk_block_deal.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_bulk_block_deals(adapter, ctx, doc, tally)

        assert tally.raw_files == 1
        assert tally.rows_written == 2
        rows = ctx.session.add_all.call_args[0][0]
        r0 = rows[0]
        assert r0.isin == "INE848E01016"
        assert r0.deal_date == date(2024, 7, 5)
        assert r0.quantity == Decimal("200000")
        assert r0.price_per_share == Decimal("875.00")
        assert r0.model_version is None

    def test_missing_deal_side_rejected(self):
        bad_csv = b"isin,deal_type,deal_date,client_name,quantity,price_per_share\nINE848E01016,bulk,2024-07-05,Client,1000,100.00\n"
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_bulk_block_deals(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1


class TestScheduledEventAdapter:
    def test_happy_path(self):
        doc = _make_doc(FIXTURES / "scheduled_event.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_scheduled_events(adapter, ctx, doc, tally)

        assert tally.raw_files == 1
        assert tally.rows_written == 3
        rows = ctx.session.add_all.call_args[0][0]
        r0 = rows[0]
        assert r0.isin == "INE848E01016"
        assert r0.event_date == date(2024, 7, 25)
        assert r0.description == "Q1 FY25 earnings announcement"
        assert r0.outcome is None
        assert r0.model_version is None
        r1 = rows[1]
        assert r1.outcome == "Meeting held; all resolutions passed"

    def test_empty_optional_fields_become_none(self):
        doc = _make_doc(FIXTURES / "scheduled_event.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_scheduled_events(adapter, ctx, doc, tally)

        rows = ctx.session.add_all.call_args[0][0]
        r2 = rows[2]
        assert r2.description is None
        assert r2.outcome is None


class TestIndexEventAdapter:
    def test_happy_path(self):
        doc = _make_doc(FIXTURES / "index_event.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_index_events(adapter, ctx, doc, tally)

        assert tally.raw_files == 1
        assert tally.rows_written == 3
        rows = ctx.session.add_all.call_args[0][0]
        r0 = rows[0]
        assert r0.isin == "INE848E01016"
        assert r0.announced_date == date(2024, 3, 1)
        assert r0.effective_date == date(2024, 3, 28)
        assert r0.old_weight == Decimal("1.25")
        assert r0.new_weight == Decimal("1.45")
        assert r0.model_version is None

    def test_optional_weight_fields_are_none(self):
        doc = _make_doc(FIXTURES / "index_event.csv")
        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()

        _load_index_events(adapter, ctx, doc, tally)

        rows = ctx.session.add_all.call_args[0][0]
        r1 = rows[1]
        assert r1.old_weight is None
        assert r1.new_weight == Decimal("0.35")
        r2 = rows[2]
        assert r2.effective_date is None
        assert r2.old_weight is None
        assert r2.new_weight is None

    def test_bad_index_code_rejected(self):
        bad_csv = (
            b"isin,index_code,event_type,status,announced_date\n"
            b"INE848E01016,not_an_index,inclusion,announced,2024-01-01\n"
        )
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_index_events(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1

    def test_missing_required_column_rejected(self):
        bad_csv = (
            b"isin,index_code,event_type,announced_date\n"
            b"INE848E01016,nifty_50,inclusion,2024-01-01\n"
        )
        doc = MagicMock()
        doc.as_of = AS_OF
        doc.content_hash = CONTENT_HASH
        doc.source_url = SOURCE_URL
        doc.data = bad_csv

        adapter = _make_adapter()
        ctx = _make_ctx()
        tally = Tally()
        _load_index_events(adapter, ctx, doc, tally)

        assert tally.rejected_files == 1


class TestTally:
    def test_ok_result_when_no_problems(self):
        from ingest.base import RunStatus

        t = Tally(raw_files=3, rows_written=10)
        result = t.result("test_adapter")
        assert result.status == RunStatus.SUCCEEDED
        assert result.rows_written == 10

    def test_partial_result_when_problems(self):
        from ingest.base import RunStatus

        t = Tally(raw_files=2, rows_written=5, rejected_files=1, problems=["bad file"])
        result = t.result("test_adapter")
        assert result.status == RunStatus.FAILED

    def test_model_version_never_set(self):
        """model_version must be None on every row produced by the adapters.

        This verifies rule R3 — LLM outputs are hypotheses — and the
        constraint ck_*_no_model in the migration.
        """
        for fixture, loader in [
            ("insider_trade.csv", _load_insider_trades),
            ("stake_disclosure.csv", _load_stake_disclosures),
            ("bulk_block_deal.csv", _load_bulk_block_deals),
            ("scheduled_event.csv", _load_scheduled_events),
            ("index_event.csv", _load_index_events),
        ]:
            doc = _make_doc(FIXTURES / fixture)
            adapter = _make_adapter()
            ctx = _make_ctx()
            tally = Tally()
            loader(adapter, ctx, doc, tally)
            assert tally.rejected_files == 0, f"{fixture} had unexpected rejections"
            rows = ctx.session.add_all.call_args[0][0]
            for row in rows:
                assert row.model_version is None, (
                    f"{fixture}: row {row!r} has model_version={row.model_version!r}, "
                    "expected None"
                )
