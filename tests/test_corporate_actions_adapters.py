"""Corporate actions in the database: drop-folder adapters, versions, lineage and price adjustment at `t`."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    CorporateAction,
    CorporateActionQuarantine,
    CorporateActionStatus,
    CorporateActionType,
    NseBhavcopyRow,
    RatioBasis,
)
from core.db.models import CorporateActionQuarantineReason as Reason
from core.db.pit import (
    adjusted_closes_as_of,
    corporate_action_quarantine_review_as_of,
    corporate_actions_as_of,
    isin_lineage_as_of,
    price_adjustments_as_of,
)
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.corporate_actions.adapters import CorporateActionsCuratedDrop, NseCorporateActionsDrop
from ingest.corporate_actions.parser import NSE_COLUMNS, RULE_VERSION
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

D = Decimal
RELIANCE, INFY, INFY_NEW = "INE002A01018", "INE009A01021", "INE009A01039"
FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"
DOWNLOADED = datetime(2024, 10, 1, 20, 0, tzinfo=IST)
LATER = DOWNLOADED + timedelta(days=1)
NSE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


def _ctx(session, tmp_path, now=DOWNLOADED + timedelta(hours=1), blob=None) -> AdapterContext:
    settings = Settings(
        drop_folder=tmp_path,
        source_switches={"nse_corporate_actions_drop": True, "corporate_actions_curated_drop": True},
    )
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _drop(tmp_path: Path, adapter: str, name: str, data: bytes, published_at: datetime, url: str = NSE_URL):
    folder = tmp_path / adapter
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": url, "published_at": published_at.isoformat(), "media_type": "text/csv"})
    )


def _bhav(session, symbol: str, isin: str, day: date, close: str = "100") -> None:
    session.add(
        NseBhavcopyRow(
            trade_date=day, isin=isin, symbol=symbol, series="EQ", open=D(close), high=D(close), low=D(close),
            close=D(close), prev_close=D(close), volume=1000, turnover=D(close) * 1000, trades=10,
            rule_version="t", as_of=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=IST),
            content_hash=content_hash(f"{symbol}{day}".encode()), source_url=f"bhav-{day}",
            extracted_by="tests", model_version=None,
        )  # fmt: skip
    )
    session.flush()


_seq = iter(range(1, 10_000))


def _action(session, *, key=None, isin=INFY, kind=CorporateActionType.BONUS, as_of, ex_date,
            status=CorporateActionStatus.DATES_SET, basis=RatioBasis.EXCHANGE_FIELD, **terms) -> CorporateAction:  # fmt: skip
    n = next(_seq)
    row = CorporateAction(
        action_key=key or f"test:{n}", isin=isin, action_type=kind, status=status, ex_date=ex_date,
        ratio_basis=basis, verified_by="reviewer" if basis is RatioBasis.HUMAN_VERIFIED else None,
        source_row=1, rule_version="t", as_of=as_of, content_hash=content_hash(str(n).encode()),
        source_url="https://example.org/evidence", extracted_by="tests", model_version=None, **terms,
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def _t(day: date, hour: int = 20) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=IST)


# --------------------------------------------------------------------------- #
# NSE export drop folder
# --------------------------------------------------------------------------- #


def test_nse_export_loads_resolved_rows_and_quarantines_the_rest(session, tmp_path):
    for symbol, isin in (("RELIANCE", RELIANCE), ("INFY", INFY)):
        _bhav(session, symbol, isin, date(2024, 8, 1))
    _drop(tmp_path, "nse_corporate_actions_drop", "ca.csv",
          (FIXTURES / "nse_corporate_actions_sample.csv").read_bytes(), DOWNLOADED)  # fmt: skip
    result = NseCorporateActionsDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert result.rows_written == 5 and result.quarantined == 3, result.detail

    rows = {r.action_key: r for r in session.scalars(select(CorporateAction))}
    bonus = rows[f"nse:{RELIANCE}:bonus:2024-10-28:1"]
    assert (bonus.shares_new, bonus.shares_held, bonus.as_of) == (1, 1, DOWNLOADED)
    assert bonus.ratio_basis is RatioBasis.EXCHANGE_FIELD and bonus.source_row == 1
    assert rows[f"nse:{RELIANCE}:dividend:2024-08-19:1"].as_of == datetime(2024, 8, 19, tzinfo=IST)
    reasons = sorted(q.reason for q in session.scalars(select(CorporateActionQuarantine)))
    assert reasons == sorted([Reason.RATIO_NOT_IN_EXCHANGE_FIELD, Reason.UNPARSED_PURPOSE, Reason.MALFORMED_ROW])


def test_rerun_is_a_no_op_until_a_symbol_becomes_resolvable(session, tmp_path):
    row = "NEWCO,New Company,EQ,Bonus 1:2,2,28-Oct-2024,28-Oct-2024,-,-"
    data = ("\n".join([",".join(NSE_COLUMNS), row]) + "\n").encode()
    _drop(tmp_path, "nse_corporate_actions_drop", "ca.csv", data, DOWNLOADED)
    adapter, blob = NseCorporateActionsDrop(), MemoryBlobStore()
    first = adapter.run(_ctx(session, tmp_path, blob=blob))
    assert (first.rows_written, first.quarantined) == (0, 1)
    again = adapter.run(_ctx(session, tmp_path, now=LATER, blob=blob))
    assert (again.rows_written, again.quarantined) == (0, 0), again.detail

    review = corporate_action_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=_t(date(2024, 11, 1)))
    assert [e.needs_review for e in review] == [True]

    _bhav(session, "NEWCO", "INE000A01012", date(2024, 9, 30))  # published before the row's as_of
    retried = adapter.reparse(_ctx(session, tmp_path, now=LATER, blob=blob))
    assert (retried.rows_written, retried.quarantined) == (1, 0), retried.detail
    review = corporate_action_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=_t(date(2024, 11, 1)))
    assert [e.needs_review for e in review] == [False]


def test_symbol_resolves_to_the_isin_it_traded_under_before_the_ex_date(session, tmp_path):
    _bhav(session, "INFY", INFY, date(2024, 9, 1))
    _bhav(session, "INFY", INFY_NEW, date(2024, 11, 1))  # later reuse of the symbol: not known, not relevant
    row = "INFY,Infosys,EQ,Bonus 1:1,5,28-Oct-2024,28-Oct-2024,-,-"
    _drop(tmp_path, "nse_corporate_actions_drop", "ca.csv",
          ("\n".join([",".join(NSE_COLUMNS), row]) + "\n").encode(), DOWNLOADED)  # fmt: skip
    NseCorporateActionsDrop().run(_ctx(session, tmp_path))
    assert session.scalar(select(CorporateAction.isin)) == INFY


def test_a_changed_header_rejects_the_file_once(session, tmp_path):
    _drop(tmp_path, "nse_corporate_actions_drop", "ca.csv", b"SYMBOL,PURPOSE\nINFY,Bonus 1:1\n", DOWNLOADED)
    adapter, blob = NseCorporateActionsDrop(), MemoryBlobStore()
    result = adapter.run(_ctx(session, tmp_path, blob=blob))
    assert result.status is RunStatus.FAILED and result.quarantined == 1
    adapter.run(_ctx(session, tmp_path, now=LATER, blob=blob))
    (entry,) = session.scalars(select(CorporateActionQuarantine))
    assert entry.reason is Reason.SHAPE_CHANGED and entry.row_number is None


def test_disabled_adapter_writes_nothing(session, tmp_path):
    ctx = _ctx(session, tmp_path)
    ctx.settings = Settings(drop_folder=tmp_path, source_switches={})
    assert NseCorporateActionsDrop().run(ctx).status is RunStatus.DISABLED


# --------------------------------------------------------------------------- #
# Curated drop folder
# --------------------------------------------------------------------------- #


def test_curated_rows_take_their_announcement_time_and_evidence(session, tmp_path):
    saved = datetime(2024, 10, 2, 9, 0, tzinfo=IST)
    _drop(tmp_path, "corporate_actions_curated_drop", "curated.csv",
          (FIXTURES / "curated_sample.csv").read_bytes(), saved, url="file://family/curated.csv")  # fmt: skip
    result = CorporateActionsCuratedDrop().run(_ctx(session, tmp_path, now=saved))
    assert (result.rows_written, result.quarantined) == (2, 1), result.detail
    demerger = session.scalar(select(CorporateAction).where(CorporateAction.action_type == CorporateActionType.DEMERGER))
    assert demerger.as_of == datetime(2024, 8, 20, 18, 30, tzinfo=IST)
    assert demerger.source_url.startswith("https://www.bseindia.com/")
    assert demerger.ratio_basis is RatioBasis.HUMAN_VERIFIED and demerger.verified_by == "family-reviewer"


# --------------------------------------------------------------------------- #
# Versions and price adjustment at t
# --------------------------------------------------------------------------- #

EX = date(2024, 10, 28)


def test_an_action_changes_nothing_before_it_is_known_or_effective(session):
    _action(session, as_of=_t(date(2024, 9, 5)), ex_date=EX, shares_new=1, shares_held=1)
    assert price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 9, 4))).applied == ()
    pending = price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 10, 1)))
    assert pending.applied == () and "after" in pending.skipped[0].reason
    (applied,) = price_adjustments_as_of(session, isin=INFY, as_of=_t(EX, 9)).applied
    assert applied.factor.factor == D("0.5") and applied.factor.ex_date == EX


def test_newest_version_wins_and_withdrawal_removes_it(session):
    first = _action(session, key="k", as_of=_t(date(2024, 9, 5)), ex_date=EX, shares_new=1, shares_held=1)
    _action(session, key="k", as_of=_t(date(2024, 9, 20)), ex_date=EX, shares_new=1, shares_held=2,
            status=CorporateActionStatus.REVISED)  # fmt: skip
    _action(session, key="k", as_of=_t(date(2024, 10, 10)), ex_date=None, status=CorporateActionStatus.WITHDRAWN)
    at = {d: corporate_actions_as_of(session, isins=[INFY], as_of=_t(d)) for d in
          (date(2024, 9, 10), date(2024, 9, 25), date(2024, 10, 15))}  # fmt: skip
    assert at[date(2024, 9, 10)] == [first]
    assert at[date(2024, 9, 25)][0].shares_held == 2
    assert at[date(2024, 10, 15)][0].status is CorporateActionStatus.WITHDRAWN
    assert price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 11, 1))).applied == ()


def test_an_unverified_ratio_adjusts_nothing_until_verified(session):
    _action(session, key="d", kind=CorporateActionType.DEMERGER, as_of=_t(date(2024, 8, 20)), ex_date=EX,
            basis=RatioBasis.UNVERIFIED, retained_fraction=D("0.85"))  # fmt: skip
    after = _t(date(2024, 11, 1))
    unverified = price_adjustments_as_of(session, isin=INFY, as_of=after)
    assert unverified.applied == () and "verification" in unverified.skipped[0].reason
    # A person checks the ratio: a new version, dated when the terms were announced.
    _action(session, key="d", kind=CorporateActionType.DEMERGER, as_of=_t(date(2024, 8, 20)), ex_date=EX,
            basis=RatioBasis.HUMAN_VERIFIED, retained_fraction=D("0.85"))  # fmt: skip
    (applied,) = price_adjustments_as_of(session, isin=INFY, as_of=after).applied
    assert applied.factor.factor == D("0.85")


def test_dividends_never_adjust_prices(session):
    _action(session, kind=CorporateActionType.DIVIDEND, as_of=_t(date(2024, 9, 5)), ex_date=EX,
            dividend_per_share=D(21))  # fmt: skip
    adjustments = price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 11, 1)))
    assert adjustments.applied == () and adjustments.skipped == ()


def test_rights_factor_uses_the_last_cum_rights_close(session):
    ex = date(2024, 9, 2)
    _bhav(session, "INFY", INFY, date(2024, 8, 29), close="90")
    _bhav(session, "INFY", INFY, date(2024, 8, 30), close="100")
    _action(session, kind=CorporateActionType.RIGHTS, as_of=_t(date(2024, 8, 1)), ex_date=ex,
            shares_new=1, shares_held=4, issue_price=D(60))  # fmt: skip
    (applied,) = price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 9, 3))).applied
    assert applied.cum_close == D(100) and applied.factor.factor == D("0.92")


def test_rights_without_a_cum_close_is_skipped_with_a_reason(session):
    _action(session, kind=CorporateActionType.RIGHTS, as_of=_t(date(2024, 8, 1)), ex_date=date(2024, 9, 2),
            shares_new=1, shares_held=4, issue_price=D(60))  # fmt: skip
    adjustments = price_adjustments_as_of(session, isin=INFY, as_of=_t(date(2024, 9, 3)))
    assert adjustments.applied == () and "no close" in adjustments.skipped[0].reason


def test_isin_lineage_and_adjusted_series_across_an_isin_change(session):
    split_ex, change_ex = date(2024, 9, 12), date(2024, 9, 13)
    _bhav(session, "INFY", INFY, date(2024, 9, 11), close="1000")
    _bhav(session, "INFY", INFY, split_ex, close="200")
    _bhav(session, "INFY", INFY_NEW, change_ex, close="202")
    _action(session, kind=CorporateActionType.SPLIT, as_of=_t(date(2024, 8, 1)), ex_date=split_ex,
            face_value_from=D(10), face_value_to=D(2))  # fmt: skip
    _action(session, kind=CorporateActionType.ISIN_CHANGE, as_of=_t(date(2024, 9, 1)), ex_date=change_ex,
            basis=RatioBasis.HUMAN_VERIFIED, new_isin=INFY_NEW)  # fmt: skip

    assert isin_lineage_as_of(session, isin=INFY_NEW, as_of=_t(date(2024, 9, 12))) == [INFY_NEW]  # not yet effective
    after = _t(date(2024, 9, 20))
    assert isin_lineage_as_of(session, isin=INFY_NEW, as_of=after) == [INFY_NEW, INFY]
    closes = adjusted_closes_as_of(session, isin=INFY_NEW, start=date(2024, 9, 1), end=date(2024, 9, 30), as_of=after)
    assert closes == {date(2024, 9, 11): D(200), split_ex: D(200), change_ex: D(202)}

    # Known before the split took effect: the raw series, unadjusted.
    before = adjusted_closes_as_of(session, isin=INFY, start=date(2024, 9, 1), end=date(2024, 9, 30),
                                   as_of=datetime(2024, 9, 11, 23, 59, 59, tzinfo=IST))  # fmt: skip
    assert before == {date(2024, 9, 11): D(1000)}


# --------------------------------------------------------------------------- #
# Database constraints
# --------------------------------------------------------------------------- #


def _rejected(session, exc=IntegrityError, **kwargs):
    with pytest.raises(exc):
        with session.begin_nested():
            _action(session, **kwargs)


def test_constraints(session):
    known = _t(date(2024, 9, 1))
    _rejected(session, as_of=known, ex_date=EX, shares_new=0, shares_held=1)  # impossible bonus
    _rejected(session, kind=CorporateActionType.SPLIT, as_of=known, ex_date=EX,
              face_value_from=D(2), face_value_to=D(10))  # a split that grows the face value  # fmt: skip
    _rejected(session, as_of=_t(date(2024, 10, 29)), ex_date=EX, shares_new=1, shares_held=1)  # known after ex-date
    _rejected(session, as_of=known, ex_date=None, shares_new=1, shares_held=1)  # final status, no ex-date
    _rejected(session, kind=CorporateActionType.ISIN_CHANGE, as_of=known, ex_date=EX, new_isin=INFY)  # to itself
    with pytest.raises(IntegrityError), session.begin_nested():  # human-verified with no name
        session.execute(
            text(
                "INSERT INTO corporate_action (action_key, isin, action_type, status, ex_date, shares_new,"
                " shares_held, ratio_basis, source_row, rule_version, as_of, content_hash, source_url,"
                " extracted_by) VALUES ('x', :isin, 'bonus', 'dates_set', :ex, 1, 1, 'human_verified', 1, 't',"
                " :as_of, :h, 'u', 'tests')"
            ),
            {"isin": INFY, "ex": EX, "as_of": known, "h": content_hash(b"x")},
        )
    # An announcement may lack terms and an ex-date.
    _action(session, as_of=known, ex_date=None, status=CorporateActionStatus.ANNOUNCED)


def test_append_only(session):
    row = _action(session, as_of=_t(date(2024, 9, 1)), ex_date=EX, shares_new=1, shares_held=1)
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text("UPDATE corporate_action SET shares_held = 2 WHERE id = :id"), {"id": row.id})
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text("DELETE FROM corporate_action WHERE id = :id"), {"id": row.id})
