"""E2E tests for the Screener schedules adapter over httpx.MockTransport.

Synthetic fixtures only (no real Screener payloads). No network: a fake
monotonic clock and sleep stand in for real time, so the throttle is checked
without waiting. Universe symbols come from stored constituent lists, ISINs
from the dated bhavcopy map, never guessed.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from core.blob import content_hash
from core.config import Settings
from core.db.models import (
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFactsQuarantineReason,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    NseBhavcopyRow,
    RawSourceFile,
)
from core.sources import DeploymentMode
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.registry import StartupRefused, check_startup, discover
from ingest.screener_schedules.adapters import ScreenerSchedules

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=IST)
TCS_ISIN = "INE467B01029"
INFY_ISIN = "INE009A01021"

PAGES = {
    "TCS": '<html><body><div data-company-id="9001">TCS</div></body></html>',
    "INFY": '<html><body><div data-company-id="9002">INFY</div></body></html>',
    "NOISIN": '<html><body><div data-company-id="9003">X</div></body></html>',
}
SCHEDULES = {
    "9001": {
        "Land": {"Mar 2024": "1,294", "Mar 2025": "2,873"},
        "Gross Block": {
            "Mar 2024": "234,567",
            "Mar 2025": "345,678",
            "setAttributes": {"class": "strong"},
        },
        "Accumulated Depreciation": {"Mar 2025": "120,000"},
    },
    "9002": {"Gross Block": {"Mar 2024": "100,000", "Mar 2025": "110,000"}},
    "9003": {"Gross Block": {"Mar 2025": "1"}},
}


class FakeTime:
    """Monotonic clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _settings(**overrides) -> Settings:
    base = dict(
        deployment_mode=DeploymentMode.PERSONAL,
        web_scraping_enabled=True,
        source_switches={"screener_schedules": True},
        screener_schedules_base_url="https://www.screener.in",
        screener_schedules_min_interval_seconds=0.0,  # the adapter must still enforce its floor
    )
    base.update(overrides)
    return Settings(**base)


def _adapter(clock: FakeTime, status: int = 200):
    """(adapter, request log of (fake-monotonic time, path))."""
    log: list[tuple[float, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((clock.monotonic(), request.url.path))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if status != 200:
            return httpx.Response(status)
        if path.startswith("/company/"):
            symbol = path.split("/")[2]
            return httpx.Response(200, text=PAGES[symbol])
        if path.startswith("/api/company/"):
            return httpx.Response(200, text=json.dumps(SCHEDULES[path.split("/")[3]]))
        return httpx.Response(404)

    adapter = ScreenerSchedules(
        transport=httpx.MockTransport(handler), monotonic=clock.monotonic, sleep=clock.sleep
    )
    return adapter, log


def _seed_universe(session, *symbols_isins: tuple[str, str | None]) -> None:
    """Constituent lists give the symbols; bhavcopy gives the dated ISIN (None: no bhavcopy row)."""
    published = datetime(2026, 9, 1, 18, 0, tzinfo=IST)
    prov = dict(
        as_of=published,
        content_hash=content_hash(b"screener-schedules-e2e-list"),
        source_url="list-e2e",
        extracted_by="tests",
        model_version=None,
    )
    snapshot = IndexSnapshot(
        index_code=IndexCode.NIFTY_50,
        constituent_count=len(symbols_isins),
        quarantined_rows=0,
        rule_version="t",
        **prov,
    )
    session.add(snapshot)
    session.flush()
    for n, (symbol, isin) in enumerate(symbols_isins, start=1):
        session.add(
            IndexSnapshotConstituent(
                snapshot_id=snapshot.id,
                isin=isin or "INE000000019",
                symbol=symbol,
                series="EQ",
                company_name="C",
                industry="I",
                row_number=n,
                **prov,
            )
        )
        if isin is not None:
            session.add(
                NseBhavcopyRow(
                    trade_date=date(2026, 9, 28),
                    isin=isin,
                    symbol=symbol,
                    series="EQ",
                    open=Decimal(1),
                    high=Decimal(1),
                    low=Decimal(1),
                    close=Decimal(1),
                    prev_close=Decimal(1),
                    volume=1,
                    turnover=Decimal(1),
                    trades=1,
                    rule_version="t",
                    as_of=datetime(2026, 9, 28, 23, 59, tzinfo=IST),
                    content_hash=content_hash(f"bhav-{symbol}".encode()),
                    source_url=f"bhav-{symbol}",
                    extracted_by="tests",
                    model_version=None,
                )
            )
    session.flush()


def _facts(session):
    return list(session.scalars(select(FinancialFact).order_by(FinancialFact.isin)))


def test_full_ingest_flow(session, s3_store):
    _seed_universe(session, ("TCS", TCS_ISIN), ("INFY", INFY_ISIN))
    adapter, _ = _adapter(FakeTime())
    ctx = AdapterContext(session, s3_store, _settings(), lambda: NOW)

    result = adapter.run(ctx)

    assert result.status is RunStatus.SUCCEEDED
    assert result.rows_written == 4  # two periods each
    all_facts = _facts(session)
    assert sorted((f.isin, f.period_end) for f in all_facts) == sorted(
        [
            (TCS_ISIN, date(2024, 3, 31)),
            (TCS_ISIN, date(2025, 3, 31)),
            (INFY_ISIN, date(2024, 3, 31)),
            (INFY_ISIN, date(2025, 3, 31)),
        ]
    )
    assert all(f.as_of == NOW for f in all_facts)
    by_period = {(f.isin, f.period_end): f for f in all_facts}
    assert by_period[(TCS_ISIN, date(2024, 3, 31))].value == Decimal("2345670000000")
    facts = {f.isin: f for f in all_facts if f.period_end == date(2025, 3, 31)}
    assert facts[TCS_ISIN].value == Decimal("3456780000000")  # 345,678 crore
    assert facts[TCS_ISIN].period_end == date(2025, 3, 31)
    assert facts[TCS_ISIN].line_item == "property_plant_and_equipment_gross"
    assert facts[INFY_ISIN].value == Decimal("1100000000000")
    # Raw bytes were stored for both symbols (company page and schedules).
    raw = session.scalar(
        select(func.count())
        .select_from(RawSourceFile)
        .where(RawSourceFile.extracted_by == adapter.extracted_by())
    )
    assert raw == 4


def test_throttle_at_least_three_seconds_between_all_requests(session, s3_store):
    _seed_universe(session, ("TCS", TCS_ISIN), ("INFY", INFY_ISIN))
    adapter, log = _adapter(FakeTime())
    adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))

    paths = [p for _, p in log]
    assert any(p.startswith("/company/") for p in paths)
    assert any(p.startswith("/api/company/") for p in paths)
    assert len(log) >= 5  # robots + 2 pages + 2 schedules
    gaps = [b[0] - a[0] for a, b in zip(log, log[1:])]
    assert all(g >= 3.0 for g in gaps), gaps


def test_idempotent_rerun_writes_nothing(session, s3_store):
    _seed_universe(session, ("TCS", TCS_ISIN), ("INFY", INFY_ISIN))
    adapter, _ = _adapter(FakeTime())
    first = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))
    assert first.rows_written == 4
    before = len(_facts(session))

    later = NOW + timedelta(days=1)  # a later clock: the raw-file key includes fetched_at
    second = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: later))

    assert second.status is RunStatus.SUCCEEDED
    assert second.rows_written == 0
    assert len(_facts(session)) == before


def test_unresolved_isin_is_quarantined_never_guessed(session, s3_store):
    _seed_universe(session, ("NOISIN", None))
    adapter, log = _adapter(FakeTime())

    result = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))

    assert result.status is RunStatus.SUCCEEDED
    assert result.rows_written == 0
    assert _facts(session) == []
    rows = list(session.scalars(select(FinancialFactsQuarantine)))
    assert len(rows) == 1
    assert rows[0].reason is FinancialFactsQuarantineReason.ISIN_UNRESOLVED
    assert rows[0].isin is None
    assert not any(p.startswith("/api/company/") for _, p in log)  # no schedules fetch without an ISIN


def test_blocked_access_stops_the_run(session, s3_store):
    _seed_universe(session, ("TCS", TCS_ISIN), ("INFY", INFY_ISIN))
    adapter, log = _adapter(FakeTime(), status=403)

    result = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))

    assert result.status is RunStatus.FAILED
    assert _facts(session) == []
    company_requests = [p for _, p in log if p.startswith("/company/")]
    assert len(company_requests) == 1  # stopped at the first block: no retry, no second symbol


def test_off_by_default(session, s3_store):
    adapter, log = _adapter(FakeTime())
    ctx = AdapterContext(session, s3_store, Settings(), lambda: NOW)
    assert adapter.run(ctx).status is RunStatus.DISABLED
    assert log == []


def test_forced_off_in_commercial_mode():
    adapters = discover()
    assert "screener_schedules" in adapters
    settings = _settings(deployment_mode=DeploymentMode.COMMERCIAL, web_scraping_enabled=False)
    with pytest.raises(StartupRefused, match="screener_schedules"):
        check_startup(settings, adapters)


def _gross(session, isin):
    return sorted(
        (f.period_end, f.value, f.as_of)
        for f in session.scalars(select(FinancialFact).where(FinancialFact.isin == isin))
    )


def test_restated_period_is_a_new_version_and_old_one_stays(session, s3_store):
    _seed_universe(session, ("INFY", INFY_ISIN))
    adapter, _ = _adapter(FakeTime())
    adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))

    later = NOW + timedelta(days=30)
    saved = dict(SCHEDULES["9002"]["Gross Block"])
    try:
        SCHEDULES["9002"]["Gross Block"]["Mar 2024"] = "101,000"  # restated; Mar 2025 unchanged
        second = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: later))
    finally:
        SCHEDULES["9002"]["Gross Block"] = saved

    assert second.rows_written == 1
    assert _gross(session, INFY_ISIN) == [
        (date(2024, 3, 31), Decimal("1000000000000"), NOW),
        (date(2024, 3, 31), Decimal("1010000000000"), later),
        (date(2025, 3, 31), Decimal("1100000000000"), NOW),
    ]


def test_new_period_on_rerun_writes_only_that_period(session, s3_store):
    _seed_universe(session, ("INFY", INFY_ISIN))
    adapter, _ = _adapter(FakeTime())
    adapter.run(AdapterContext(session, s3_store, _settings(), lambda: NOW))

    later = NOW + timedelta(days=400)
    saved = dict(SCHEDULES["9002"]["Gross Block"])
    try:
        SCHEDULES["9002"]["Gross Block"]["Mar 2026"] = "120,000"
        second = adapter.run(AdapterContext(session, s3_store, _settings(), lambda: later))
    finally:
        SCHEDULES["9002"]["Gross Block"] = saved

    assert second.rows_written == 1
    assert len(_gross(session, INFY_ISIN)) == 3
