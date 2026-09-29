"""Order-win adapter over already-ingested NSE announcement raw files (Session 7e).

No network: the adapter reads raw listings the 7c adapter stored, so the tests
store listings through `NseAnnouncements.store_raw` and never fetch.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.compute.order_wins import RULE_VERSION
from core.config import Settings
from core.db.models import (
    OrderWinFact,
    OrderWinQuarantine,
    OrderWinQuarantineReason,
    RawSourceFile,
)
from core.db.pit import order_wins_as_of
from core.sources import DeploymentMode, SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, CanaryStatus, RunStatus
from ingest.nse_announcements.adapters import NseAnnouncements
from ingest.nse_order_wins.adapters import NseOrderWins
from ingest.registry import StartupRefused, check_startup, discover

pytestmark = [pytest.mark.db, pytest.mark.blob]

ISIN = "INE002A01018"
NOW = datetime(2026, 5, 10, 12, 0, tzinfo=IST)
LISTING_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"

GOOD = {
    "subject": "Receipt of Order",
    "description": (
        "The Company has received an order from Indian Railways for supply of 40 "
        "locomotives worth Rs. 2,500 crore, to be executed within 24 months."
    ),
    "announced_on": "2026-05-04 10:30:00",
    "isin": ISIN,
    "symbol": "RELIANCE",
}
BARE = {
    "subject": "Order win",
    "description": "The Company has won an order worth Rs 50 crore.",
    "announced_on": "2026-05-04 11:00:00",
    "isin": ISIN,
    "symbol": "RELIANCE",
}
NO_VALUE = {
    "subject": "Receipt of order",
    "description": "Received an order from BHEL.",
    "announced_on": "2026-05-04 12:00:00",
    "isin": ISIN,
    "symbol": "RELIANCE",
}
PRE_AWARD = {
    "subject": "Emerges as lowest bidder",
    "description": "The Company is the lowest bidder for a project worth Rs 100 crore.",
    "announced_on": "2026-05-04 13:00:00",
    "isin": ISIN,
    "symbol": "RELIANCE",
}
NOT_AN_ORDER = {
    "subject": "Board Meeting to be held on 15-Feb-2026",
    "description": "To consider quarterly results.",
    "announced_on": "2026-05-04 14:00:00",
    "isin": ISIN,
    "symbol": "RELIANCE",
}
BAD_ISIN = {**GOOD, "announced_on": "2026-05-04 15:00:00", "isin": "BADISIN"}


def _settings(**overrides) -> Settings:
    base = dict(
        deployment_mode=DeploymentMode.PERSONAL,
        web_scraping_enabled=True,
        source_switches={"nse_order_wins": True},
    )
    base.update(overrides)
    return Settings(**base)


def _ctx(session, s3_store, *, settings: Settings | None = None, now: datetime = NOW) -> AdapterContext:
    return AdapterContext(
        session=session, blob=s3_store, settings=settings or _settings(), now=lambda: now
    )


def _store_listing(ctx: AdapterContext, rows: list[dict], *, as_of: datetime, tag: str = "") -> None:
    """What the 7c adapter leaves behind: the raw API response, recorded in raw_source_file."""
    payload = json.dumps({"data": rows, "tag": tag}).encode()
    NseAnnouncements().store_raw(
        ctx, data=payload, source_url=LISTING_URL, as_of=as_of, media_type="application/json"
    )


def _seed(session, s3_store, rows, *, tag: str) -> AdapterContext:
    ctx = _ctx(session, s3_store)
    _store_listing(ctx, rows, as_of=datetime(2026, 5, 4, 16, 0, tzinfo=IST), tag=tag)
    return ctx


# --------------------------------------------------------------------------- #
# Declaration and switches
# --------------------------------------------------------------------------- #


def test_declares_its_source():
    assert NseOrderWins.name == "nse_order_wins"
    assert NseOrderWins.source_class is SourceClass.WEB_SCRAPE
    assert NseOrderWins.target_stores == ("order_win", "order_win_quarantine")
    assert NseOrderWins.switch_name() == "EQUITY_SOURCE_NSE_ORDER_WINS_ENABLED"


def test_is_discovered_by_the_registry():
    assert discover()["nse_order_wins"] is NseOrderWins


def test_default_off_without_its_switch(session, s3_store):
    ctx = _ctx(session, s3_store, settings=_settings(source_switches={}))
    result = NseOrderWins().run(ctx)
    assert result.status is RunStatus.DISABLED
    assert "EQUITY_SOURCE_NSE_ORDER_WINS_ENABLED" in result.detail


def test_off_without_the_web_scraping_master_switch(session, s3_store):
    ctx = _ctx(session, s3_store, settings=_settings(web_scraping_enabled=False))
    assert NseOrderWins().run(ctx).status is RunStatus.DISABLED


def test_refused_at_startup_in_commercial_mode_when_enabled():
    settings = _settings(deployment_mode=DeploymentMode.COMMERCIAL, web_scraping_enabled=False)
    with pytest.raises(StartupRefused, match="nse_order_wins"):
        check_startup(settings, discover())


def test_disabled_writes_nothing(session, s3_store):
    _seed(session, s3_store, [GOOD], tag="disabled")
    off = _ctx(session, s3_store, settings=_settings(source_switches={}))
    NseOrderWins().run(off)
    assert session.scalars(select(OrderWinFact)).all() == []


# --------------------------------------------------------------------------- #
# Extraction into order_win and the quarantine
# --------------------------------------------------------------------------- #


def test_writes_order_wins_with_quotes_and_provenance(session, s3_store):
    ctx = _seed(session, s3_store, [GOOD], tag="one")
    result = NseOrderWins().run(ctx)
    assert result.status is RunStatus.SUCCEEDED
    assert result.rows_written == 1

    (row,) = order_wins_as_of(session, isin=ISIN, as_of=NOW)
    assert row.order_value_inr == Decimal("25000000000")
    assert isinstance(row.order_value_inr, Decimal)
    assert "2,500 crore" in row.value_quote
    assert row.counterparty == "Indian Railways"
    assert row.counterparty_quote == "Indian Railways"
    assert row.execution_months == Decimal("24")
    assert "24 months" in row.period_quote
    assert row.missing == []
    assert row.rule_version == RULE_VERSION
    assert row.announced_on == datetime(2026, 5, 4, 10, 30, tzinfo=IST)
    # as_of is when the announcement was public, not when this adapter ran.
    assert row.as_of == row.announced_on
    assert row.extracted_by == NseOrderWins.extracted_by()
    assert row.model_version is None
    assert row.source_url == LISTING_URL


def test_content_hash_links_to_the_raw_source_file(session, s3_store):
    ctx = _seed(session, s3_store, [GOOD], tag="link")
    NseOrderWins().run(ctx)
    row = session.scalars(select(OrderWinFact)).one()
    raw = session.scalars(
        select(RawSourceFile).where(RawSourceFile.content_hash == row.content_hash)
    ).one()
    assert raw.extracted_by == NseAnnouncements.extracted_by()


def test_gaps_are_named_not_filled(session, s3_store):
    ctx = _seed(session, s3_store, [BARE], tag="bare")
    NseOrderWins().run(ctx)
    row = session.scalars(select(OrderWinFact)).one()
    assert row.counterparty is None and row.counterparty_quote is None
    assert row.execution_months is None and row.period_quote is None
    assert set(row.missing) == {"counterparty", "execution_period"}


def test_quarantines_are_written_with_reason_and_quote(session, s3_store):
    ctx = _seed(session, s3_store, [NO_VALUE, PRE_AWARD], tag="quar")
    result = NseOrderWins().run(ctx)
    assert result.rows_written == 0
    assert result.quarantined == 2
    assert session.scalars(select(OrderWinFact)).all() == []
    rows = {q.reason: q for q in session.scalars(select(OrderWinQuarantine))}
    assert set(rows) == {OrderWinQuarantineReason.NO_VALUE, OrderWinQuarantineReason.PRE_AWARD}
    assert "Received an order from BHEL" in rows[OrderWinQuarantineReason.NO_VALUE].quote
    assert rows[OrderWinQuarantineReason.NO_VALUE].isin == ISIN
    assert rows[OrderWinQuarantineReason.NO_VALUE].rule_version == RULE_VERSION


def test_non_orders_are_counted_but_not_stored(session, s3_store):
    ctx = _seed(session, s3_store, [NOT_AN_ORDER, GOOD], tag="noise")
    result = NseOrderWins().run(ctx)
    assert result.status is RunStatus.SUCCEEDED
    assert result.rows_written == 1
    assert result.quarantined == 0
    assert "1 not an order" in result.detail
    assert session.scalars(select(OrderWinQuarantine)).all() == []


def test_invalid_isin_is_quarantined_never_written(session, s3_store):
    ctx = _seed(session, s3_store, [BAD_ISIN], tag="isin")
    result = NseOrderWins().run(ctx)
    assert result.rows_written == 0 and result.quarantined == 1
    (q,) = session.scalars(select(OrderWinQuarantine)).all()
    assert q.reason is OrderWinQuarantineReason.INVALID_ISIN
    assert q.isin is None
    assert "BADISIN" in q.quote


def test_malformed_raw_file_fails_the_run_but_good_files_are_still_read(session, s3_store):
    ctx = _ctx(session, s3_store)
    NseAnnouncements().store_raw(
        ctx,
        data=b"<html>not json</html>",
        source_url=LISTING_URL,
        as_of=datetime(2026, 5, 3, 16, 0, tzinfo=IST),
        media_type="text/html",
    )
    _store_listing(ctx, [GOOD], as_of=datetime(2026, 5, 4, 16, 0, tzinfo=IST), tag="after-bad")
    result = NseOrderWins().run(ctx)
    assert result.status is RunStatus.FAILED
    assert result.rows_written == 1


def test_only_reads_the_announcement_adapters_raw_files(session, s3_store):
    ctx = _ctx(session, s3_store)
    payload = json.dumps({"data": [GOOD], "tag": "other-source"}).encode()
    NseOrderWins().store_raw(
        ctx, data=payload, source_url="other", as_of=datetime(2026, 5, 4, 16, 0, tzinfo=IST),
        media_type="application/json",
    )
    result = NseOrderWins().run(ctx)
    assert result.rows_written == 0


def test_announcement_dated_after_now_is_a_problem_not_a_row(session, s3_store):
    future = {**GOOD, "announced_on": "2026-06-01 10:00:00"}
    ctx = _seed(session, s3_store, [future], tag="future")
    result = NseOrderWins().run(ctx)
    assert result.status is RunStatus.FAILED
    assert result.rows_written == 0
    assert session.scalars(select(OrderWinFact)).all() == []


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #


def test_rerun_writes_nothing_new(session, s3_store):
    ctx = _seed(session, s3_store, [GOOD, BARE, NO_VALUE], tag="idem")
    first = NseOrderWins().run(ctx)
    assert (first.rows_written, first.quarantined) == (2, 1)
    second = NseOrderWins().run(_ctx(session, s3_store, now=NOW + timedelta(days=1)))
    assert (second.rows_written, second.quarantined) == (0, 0)
    assert second.status is RunStatus.SUCCEEDED
    assert len(session.scalars(select(OrderWinFact)).all()) == 2
    assert len(session.scalars(select(OrderWinQuarantine)).all()) == 1


def test_same_announcement_in_an_overlapping_later_listing_is_not_duplicated(session, s3_store):
    ctx = _seed(session, s3_store, [GOOD], tag="overlap-1")
    NseOrderWins().run(ctx)
    _store_listing(ctx, [GOOD, BARE], as_of=datetime(2026, 5, 5, 16, 0, tzinfo=IST), tag="overlap-2")
    result = NseOrderWins().run(ctx)
    assert result.rows_written == 1  # only BARE is new
    assert len(session.scalars(select(OrderWinFact)).all()) == 2


# --------------------------------------------------------------------------- #
# Canary
# --------------------------------------------------------------------------- #


def test_canary_validates_the_latest_stored_listing_without_network(session, s3_store):
    ctx = _seed(session, s3_store, [GOOD], tag="canary")
    assert NseOrderWins().canary(ctx).status is CanaryStatus.PASSED


def test_canary_skipped_when_disabled(session, s3_store):
    ctx = _ctx(session, s3_store, settings=_settings(source_switches={}))
    assert NseOrderWins().canary(ctx).status is CanaryStatus.SKIPPED
