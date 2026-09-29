"""Read-only broker holdings drop: parser, quarantine, adapter, idempotence. Synthetic data only."""

import ast
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from core.config import Settings
from core.db.models import (
    BrokerHoldingQuarantine,
    BrokerHoldingQuarantineReason,
    BrokerHoldingSnapshot,
    RawSourceFile,
)
from core.db.pit import holdings_as_of
from core.sources import SourceClass
from core.timezones import IST
from ingest import registry
from ingest.base import AdapterContext, CanaryStatus, RunStatus
from ingest.broker_holdings_drop import adapters
from ingest.broker_holdings_drop.adapters import BrokerHoldingsDrop
from ingest.broker_holdings_drop.parser import HoldingsRejected, parse_holdings
from tests.fakes import MemoryBlobStore

FIXTURE = Path(__file__).parent / "fixtures" / "broker_holdings" / "synthetic_holdings.json"
NAME = "broker_holdings_drop"
ACCOUNT = "family-main"
SNAP = datetime(2026, 9, 29, 16, 0, tzinfo=IST)
KNOWN = datetime(2026, 9, 29, 16, 5, tzinfo=IST)
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=IST)
A, B, C = "INE000A01004", "INE000B01002", "INE000C01000"


def records() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def as_bytes(obj) -> bytes:
    return json.dumps(obj).encode()


def _ctx(session, tmp_path, blob=None, now=NOW, enabled=True) -> AdapterContext:
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: enabled})
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _drop(tmp_path: Path, data: bytes | None = None, *, name="holdings.json", snapshot_at=SNAP,
          published_at=KNOWN, account=ACCOUNT, **extra) -> None:  # fmt: skip
    folder = tmp_path / NAME
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data if data is not None else FIXTURE.read_bytes())
    meta = {"source_url": "manual://broker-holdings/synthetic", "published_at": published_at.isoformat(),
            "media_type": "application/json", "account_label": account,
            "snapshot_at": snapshot_at.isoformat(), **extra}  # fmt: skip
    (folder / f"{name}.meta.json").write_text(json.dumps(meta))


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------- #
# Declaration
# --------------------------------------------------------------------------- #


def test_declares_manual_drop_not_web_scrape():
    assert BrokerHoldingsDrop.name == NAME
    assert BrokerHoldingsDrop.source_class is SourceClass.MANUAL_DROP
    assert BrokerHoldingsDrop.target_stores == ("broker_holding_snapshot", "broker_holding_quarantine")
    assert registry.discover()[NAME] is BrokerHoldingsDrop


def test_adapter_package_makes_no_network_or_mcp_imports():
    banned = {"mcp", "fastmcp", "httpx", "requests", "urllib", "urllib3", "socket", "aiohttp", "http"}
    package = Path(adapters.__file__).parent
    imported = set()
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
    assert imported & banned == set()


# --------------------------------------------------------------------------- #
# Parser (no database)
# --------------------------------------------------------------------------- #


def test_parses_the_fixture_keyed_by_isin():
    parsed = parse_holdings(FIXTURE.read_bytes())
    assert parsed.quarantined == []
    assert [(r.isin, r.exchange, r.quantity) for r in parsed.rows] == [
        (A, "NSE", 100),
        (B, "NSE", 50),
        (C, "BSE", 10),
    ]


def test_money_is_exact_decimal_from_the_json_text():
    rows = {r.isin: r for r in parse_holdings(FIXTURE.read_bytes()).rows}
    assert rows[B].average_price == Decimal("1234.55")
    assert rows[B].last_price == Decimal("1300.1")
    assert rows[C].average_price == Decimal("80")  # a JSON integer is still money
    assert all(isinstance(r.average_price, Decimal) and isinstance(r.quantity, int) for r in rows.values())


def test_series_suffix_does_not_make_a_different_security():
    recs = records()
    recs[0]["tradingsymbol"] = "BETAWORKS-BE"  # same suffix as row 2, other company
    recs[1]["isin"] = A
    recs[1]["exchange"] = "BSE"
    parsed = parse_holdings(as_bytes(recs))
    # Same company on the two exchanges: two rows, one ISIN, no symbol anywhere.
    assert {r.isin for r in parsed.rows} == {A, C}
    assert not hasattr(parsed.rows[0], "tradingsymbol")


def test_accepts_a_wrapping_object():
    assert len(parse_holdings(as_bytes({"holdings": records()})).rows) == 3


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda r: r.pop("isin"), BrokerHoldingQuarantineReason.MISSING_ISIN),
        (lambda r: r.update(isin=""), BrokerHoldingQuarantineReason.MISSING_ISIN),
        (lambda r: r.update(isin=None), BrokerHoldingQuarantineReason.MISSING_ISIN),
        (lambda r: r.update(isin="NOTANISIN"), BrokerHoldingQuarantineReason.INVALID_ISIN),
        (lambda r: r.update(quantity=-3), BrokerHoldingQuarantineReason.NEGATIVE_QUANTITY),
        (lambda r: r.update(quantity="ten"), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(quantity=1.5), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(quantity=True), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.pop("quantity"), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(last_price="abc"), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(last_price=None), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(average_price=-1), BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER),
        (lambda r: r.update(exchange="NYSE"), BrokerHoldingQuarantineReason.UNKNOWN_EXCHANGE),
    ],
)
def test_bad_rows_are_quarantined_and_the_rest_kept(mutate, reason):
    recs = records()
    mutate(recs[1])
    parsed = parse_holdings(as_bytes(recs))
    assert [r.isin for r in parsed.rows] == [A, C]
    assert [(q.row_number, q.reason) for q in parsed.quarantined] == [(2, reason)]
    assert parsed.quarantined[0].detail


def test_non_finite_number_is_unparseable():
    text_ = FIXTURE.read_text().replace('"last_price": 1300.1', '"last_price": NaN')
    parsed = parse_holdings(text_.encode())
    assert [q.reason for q in parsed.quarantined] == [BrokerHoldingQuarantineReason.UNPARSEABLE_NUMBER]


def test_same_isin_and_exchange_twice_quarantines_the_second():
    recs = records()
    recs.append(dict(recs[0], quantity=999))
    parsed = parse_holdings(as_bytes(recs))
    assert [r.quantity for r in parsed.rows if r.isin == A] == [100]
    assert [(q.row_number, q.reason) for q in parsed.quarantined] == [
        (4, BrokerHoldingQuarantineReason.DUPLICATE_ROW)
    ]


@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"holdings": []}', b'{"other": 1}', b'"x"', b"[1, 2]"])
def test_a_file_that_is_not_a_holdings_list_is_rejected(data):
    with pytest.raises(HoldingsRejected):
        parse_holdings(data)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


@pytest.mark.db
def test_ingest_writes_snapshot_rows_with_provenance(session, tmp_path):
    _drop(tmp_path)
    ctx = _ctx(session, tmp_path)
    result = BrokerHoldingsDrop().run(ctx)
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.raw_files, result.rows_written, result.quarantined) == (1, 3, 0)
    rows = session.scalars(select(BrokerHoldingSnapshot).order_by(BrokerHoldingSnapshot.isin)).all()
    assert [(r.isin, r.exchange, r.quantity) for r in rows] == [(A, "NSE", 100), (B, "NSE", 50), (C, "BSE", 10)]
    assert {r.account_label for r in rows} == {ACCOUNT}
    assert {r.snapshot_at for r in rows} == {SNAP}
    assert {r.as_of for r in rows} == {KNOWN}
    assert {r.extracted_by for r in rows} == {BrokerHoldingsDrop.extracted_by()}
    assert {r.model_version for r in rows} == {None}
    raw = session.scalars(select(RawSourceFile)).one()
    assert {r.content_hash for r in rows} == {raw.content_hash}
    assert ctx.blob.get(raw.content_hash) == FIXTURE.read_bytes()
    beta = next(r for r in rows if r.isin == B)
    assert beta.average_price == Decimal("1234.55")


@pytest.mark.db
def test_rerun_is_idempotent(session, tmp_path):
    _drop(tmp_path)
    ctx = _ctx(session, tmp_path)
    BrokerHoldingsDrop().run(ctx)
    again = BrokerHoldingsDrop().run(ctx)
    assert again.status is RunStatus.SUCCEEDED
    assert again.rows_written == 0
    assert _count(session, BrokerHoldingSnapshot) == 3


@pytest.mark.db
def test_reader_sees_the_dropped_holdings(session, tmp_path):
    _drop(tmp_path)
    BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert holdings_as_of(session, account_label=ACCOUNT, as_of=SNAP) == []
    assert [r.isin for r in holdings_as_of(session, account_label=ACCOUNT, as_of=KNOWN)] == [A, B, C]


@pytest.mark.db
def test_bad_rows_go_to_quarantine_and_good_rows_are_written(session, tmp_path):
    recs = records()
    recs[0].pop("isin")
    recs[1]["quantity"] = -5
    _drop(tmp_path, as_bytes(recs))
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.SUCCEEDED
    assert (result.rows_written, result.quarantined) == (1, 2)
    assert [r.isin for r in session.scalars(select(BrokerHoldingSnapshot))] == [C]
    quarantined = session.scalars(
        select(BrokerHoldingQuarantine).order_by(BrokerHoldingQuarantine.row_number)
    ).all()
    assert [(q.row_number, q.reason, q.isin) for q in quarantined] == [
        (1, BrokerHoldingQuarantineReason.MISSING_ISIN, None),
        (2, BrokerHoldingQuarantineReason.NEGATIVE_QUANTITY, B),
    ]
    assert {q.snapshot_at for q in quarantined} == {SNAP}
    # A rerun does not quarantine the same rows again.
    BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert _count(session, BrokerHoldingQuarantine) == 2


@pytest.mark.db
def test_both_exchanges_of_one_company_are_kept_under_one_isin(session, tmp_path):
    recs = records()
    recs.append(dict(recs[0], exchange="BSE", tradingsymbol="ALPHAIND", quantity=25))
    _drop(tmp_path, as_bytes(recs))
    BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    rows = [r for r in holdings_as_of(session, account_label=ACCOUNT, as_of=KNOWN) if r.isin == A]
    assert sorted((r.exchange, r.quantity) for r in rows) == [("BSE", 25), ("NSE", 100)]


@pytest.mark.db
def test_future_snapshot_rejects_the_file(session, tmp_path):
    future = NOW + timedelta(hours=1)
    _drop(tmp_path, snapshot_at=future, published_at=future)
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.FAILED
    assert "future" in result.detail
    assert _count(session, BrokerHoldingSnapshot) == 0


@pytest.mark.db
def test_known_before_taken_rejects_the_file(session, tmp_path):
    _drop(tmp_path, snapshot_at=KNOWN, published_at=SNAP)
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.FAILED
    assert _count(session, BrokerHoldingSnapshot) == 0


@pytest.mark.db
def test_naive_snapshot_time_in_sidecar_is_refused(session, tmp_path):
    _drop(tmp_path, snapshot_at=datetime(2026, 9, 29, 16, 0))
    with pytest.raises(ValidationError):
        BrokerHoldingsDrop().run(_ctx(session, tmp_path))


@pytest.mark.db
@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"other": 1}'])
def test_unusable_file_fails_the_run_and_writes_nothing(session, tmp_path, data):
    _drop(tmp_path, data)
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.FAILED
    assert _count(session, BrokerHoldingSnapshot) == 0
    assert _count(session, BrokerHoldingQuarantine) == 0


@pytest.mark.db
def test_sidecar_needs_an_account_label(session, tmp_path):
    _drop(tmp_path, account="")
    with pytest.raises(ValidationError):
        BrokerHoldingsDrop().run(_ctx(session, tmp_path))


@pytest.mark.db
def test_disabled_writes_nothing(session, tmp_path):
    _drop(tmp_path)
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path, enabled=False))
    assert result.status is RunStatus.DISABLED
    assert _count(session, BrokerHoldingSnapshot) == 0
    assert _count(session, RawSourceFile) == 0


@pytest.mark.db
def test_canary_parses_pending_files(session, tmp_path):
    _drop(tmp_path)
    assert BrokerHoldingsDrop().canary(_ctx(session, tmp_path)).status is CanaryStatus.PASSED
    _drop(tmp_path, b"not json", name="bad.json")
    with pytest.raises(HoldingsRejected):
        BrokerHoldingsDrop().canary(_ctx(session, tmp_path))


@pytest.mark.db
def test_a_later_drop_of_the_same_account_is_a_new_snapshot(session, tmp_path):
    _drop(tmp_path)
    BrokerHoldingsDrop().run(_ctx(session, tmp_path))
    later = SNAP + timedelta(days=1)
    recs = records()
    recs[0]["quantity"] = 150
    _drop(tmp_path, as_bytes(recs), name="next.json", snapshot_at=later, published_at=later + timedelta(minutes=5))
    result = BrokerHoldingsDrop().run(_ctx(session, tmp_path, now=NOW + timedelta(days=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    rows = holdings_as_of(session, account_label=ACCOUNT, as_of=NOW + timedelta(days=1))
    assert {r.isin: r.quantity for r in rows}[A] == 150
