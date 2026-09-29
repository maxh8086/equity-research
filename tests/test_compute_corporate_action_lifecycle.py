"""Pure lifecycle, dilution and demerger-chain arithmetic. Hand-computed expectations."""

from datetime import date, datetime
from decimal import Decimal as D

import pytest

from core.compute import corporate_action_lifecycle as cal
from core.db.models import ScheduledEventType
from core.timezones import IST


def AS(d: int) -> datetime:  # noqa: N802
    return datetime(2026, 1, d, 10, tzinfo=IST)


def v(status, day, seq=1, key="k"):
    return cal.ActionVersion(action_key=key, status=status, as_of=AS(day), seq=seq)


# ---- lifecycle --------------------------------------------------------------


@pytest.mark.parametrize(
    "prev,new,ok",
    [
        ("announced", "approved", True),
        ("announced", "dates_set", True),  # exchange feeds skip steps
        ("approved", "announced", False),
        ("dates_set", "dates_set", True),  # restated terms, same status
        ("effective", "completed", True),
        ("effective", "dates_set", False),
        ("announced", "withdrawn", True),
        ("dates_set", "revised", True),
        ("completed", "revised", False),
        ("completed", "withdrawn", False),
        ("withdrawn", "announced", False),
        ("withdrawn", "withdrawn", False),
        ("completed", "completed", False),
    ],
)
def test_transitions(prev, new, ok):
    assert cal.is_allowed_transition(prev, new) is ok


def test_current_status_is_newest_by_as_of_then_seq():
    vs = [v("approved", 2, seq=5), v("announced", 1), v("dates_set", 2, seq=9)]
    assert cal.current_status(vs) == "dates_set"


def test_current_status_empty_is_none():
    assert cal.current_status([]) is None


def test_no_violations_on_clean_path():
    vs = [v("announced", 1), v("approved", 2), v("dates_set", 3), v("effective", 4), v("completed", 5)]
    assert cal.lifecycle_violations(vs) == []


def test_backward_step_is_reported_with_versions():
    vs = [v("dates_set", 1), v("approved", 2)]
    out = cal.lifecycle_violations(vs)
    assert [(x.previous, x.new, x.as_of) for x in out] == [("dates_set", "approved", AS(2))]


def test_revised_does_not_reset_progress():
    ok = [v("dates_set", 1), v("revised", 2), v("dates_set", 3)]
    assert cal.lifecycle_violations(ok) == []
    bad = [v("dates_set", 1), v("revised", 2), v("approved", 3)]
    assert len(cal.lifecycle_violations(bad)) == 1


def test_violations_checked_per_key():
    vs = [v("effective", 1, key="a"), v("announced", 2, key="b"), v("approved", 3, key="b")]
    assert cal.lifecycle_violations(vs) == []


def test_after_terminal_is_a_violation():
    vs = [v("withdrawn", 1), v("approved", 2)]
    assert len(cal.lifecycle_violations(vs)) == 1


# ---- dilution ---------------------------------------------------------------


def test_dilution_fraction_plain():
    # 100 new on 900 outstanding -> 100 / 1000
    assert cal.dilution_fraction(900, 100) == D("0.1")


def test_dilution_includes_unconverted_warrants_and_options():
    # base = 900 + 100 unconverted = 1000; 250 new -> 250 / 1250
    assert cal.dilution_fraction(900, 250, unconverted_dilutive=100) == D("0.2")


@pytest.mark.parametrize("args", [(0, 10), (10, 0), (-1, 5), (10, -5)])
def test_dilution_rejects_bad_counts(args):
    with pytest.raises(ValueError):
        cal.dilution_fraction(*args)


def test_issue_discount_positive_below_market():
    assert cal.issue_discount(D("90"), D("100")) == D("0.1")
    assert cal.issue_discount(D("110"), D("100")) == D("-0.1")


def test_issue_discount_rejects_nonpositive_market():
    with pytest.raises(ValueError):
        cal.issue_discount(D("90"), D("0"))


def test_pro_forma_eps_hand_computed():
    # earnings 1000, 1000 shares -> EPS 1.00. Issue 250 shares at 20 = 5000 raised.
    # return 10% -> +500 earnings. after: 1500 / 1250 = 1.2, so dilution is -0.2.
    r = cal.pro_forma_eps(
        earnings=D("1000"), shares_outstanding=1000, new_shares=250, issue_price=D("20"), return_rate=D("0.10")
    )
    assert r.proceeds == D("5000")
    assert r.eps_before == D("1")
    assert r.eps_after == D("1.2")
    assert r.eps_dilution_fraction == D("-0.2")
    # earnings yield at issue price = 1/20 = 5%; 10% beats it
    assert r.earnings_yield_at_issue == D("0.05")
    assert r.return_exceeds_earnings_yield is True


def test_pro_forma_eps_dilutive_when_return_low():
    # 1000 earnings/1000 shares; 1000 new at 10 = 10000; return 1% -> +100; 1100/2000 = 0.55
    r = cal.pro_forma_eps(
        earnings=D("1000"), shares_outstanding=1000, new_shares=1000, issue_price=D("10"), return_rate=D("0.01")
    )
    assert r.eps_after == D("0.55")
    assert r.eps_dilution_fraction == D("0.45")
    assert r.earnings_yield_at_issue == D("0.1")
    assert r.return_exceeds_earnings_yield is False


def test_pro_forma_counts_unconverted_in_share_base():
    r = cal.pro_forma_eps(
        earnings=D("1100"), shares_outstanding=1000, new_shares=100, issue_price=D("10"),
        return_rate=D("0"), unconverted_dilutive=100,
    )  # fmt: skip
    assert r.eps_before == D("1")  # 1100 / 1100
    assert r.eps_after == D("1100") / D("1200")


def test_pro_forma_equal_return_is_not_exceeding():
    r = cal.pro_forma_eps(
        earnings=D("1000"), shares_outstanding=1000, new_shares=100, issue_price=D("20"), return_rate=D("0.05")
    )
    assert r.return_exceeds_earnings_yield is False


def test_pro_forma_not_computable_without_positive_eps():
    with pytest.raises(cal.NotComputable):
        cal.pro_forma_eps(
            earnings=D("-5"), shares_outstanding=1000, new_shares=100, issue_price=D("20"), return_rate=D("0.05")
        )


def test_return_rate_from_debt_repayment():
    # 500 of 5000 proceeds repay debt at 8% -> 40 saved -> 0.8% on the proceeds
    assert cal.return_rate_from_debt_repayment(D("5000"), D("500"), D("0.08")) == D("0.008")


def test_return_rate_from_debt_repayment_rejects_more_debt_than_proceeds():
    with pytest.raises(ValueError):
        cal.return_rate_from_debt_repayment(D("100"), D("500"), D("0.08"))


def test_eps_gate_needs_threshold_and_no_offset():
    dilutive = cal.pro_forma_eps(
        earnings=D("1000"), shares_outstanding=1000, new_shares=1000, issue_price=D("10"), return_rate=D("0.01")
    )  # dilution 0.45
    assert cal.eps_dilution_beyond_threshold(dilutive, threshold=D("0.30")) is True
    assert cal.eps_dilution_beyond_threshold(dilutive, threshold=D("0.45")) is False  # not beyond
    accretive = cal.pro_forma_eps(
        earnings=D("1000"), shares_outstanding=1000, new_shares=250, issue_price=D("20"), return_rate=D("0.10")
    )
    assert cal.eps_dilution_beyond_threshold(accretive, threshold=D("-1")) is False  # offset by return


def test_issue_price_facts():
    f = cal.issue_price_facts(
        issue_price=D("95"), market_price=D("100"), allottee_is_promoter=True, steep_discount=D("0.05")
    )
    assert f.discount == D("0.05")
    assert f.at_or_above_market is False
    assert f.discount_at_least_steep is True
    assert f.promoter_allottee is True
    g = cal.issue_price_facts(
        issue_price=D("100"), market_price=D("100"), allottee_is_promoter=None, steep_discount=D("0.05")
    )
    assert g.at_or_above_market is True
    assert g.discount_at_least_steep is False
    assert g.promoter_allottee is None


def test_dilution_event_count_window_is_inclusive_and_pit():
    dates = [date(2026, 1, 1), date(2026, 3, 1), date(2026, 6, 30), date(2026, 7, 1)]
    # window of 120 days ending 2026-06-30 starts 2026-03-02: only 06-30 is in; 03-01 is out
    assert cal.dilution_event_count(dates, on=date(2026, 6, 30), window_days=120) == 1
    assert cal.dilution_event_count(dates, on=date(2026, 6, 30), window_days=121) == 2
    # events after `on` never count
    assert cal.dilution_event_count(dates, on=date(2026, 1, 1), window_days=30) == 1


# ---- demerger chain ---------------------------------------------------------

M = cal.DemergerMilestone


def test_milestone_order_and_event_types():
    assert [m.value for m in cal.DEMERGER_CHAIN] == [
        "scheme", "board", "shareholder", "creditor", "nclt_order", "record_date", "listing",
    ]  # fmt: skip
    valid = {e.value for e in ScheduledEventType}
    for m in cal.DEMERGER_CHAIN:
        assert cal.MILESTONE_EVENT_TYPE[m] in valid


def _plan(known, **kw):
    args = dict(today=date(2026, 5, 1), held=True, critical_within_days=7, high_within_days=30)
    args.update(kw)
    return cal.plan_demerger_chain(known, **args)


def test_plan_orders_by_chain_and_marks_pending_next():
    rows = _plan({M.NCLT_ORDER: date(2026, 4, 1), M.BOARD: date(2026, 1, 5), M.LISTING: date(2026, 6, 1)})
    assert [r.milestone for r in rows] == [M.BOARD, M.NCLT_ORDER, M.LISTING]
    assert [r.reached for r in rows] == [True, True, False]
    assert [r.days_until for r in rows] == [-116, -30, 31]


def test_plan_severity_held_by_distance():
    rows = _plan({M.RECORD_DATE: date(2026, 5, 6), M.LISTING: date(2026, 5, 25), M.NCLT_ORDER: date(2026, 9, 1)})
    sev = {r.milestone: r.severity for r in rows}
    assert sev[M.RECORD_DATE] == "critical"  # 5 days
    assert sev[M.LISTING] == "high"  # 24 days; NCLT below is before it in chain
    assert sev[M.NCLT_ORDER] == "medium"  # 123 days


def test_plan_severity_boundaries_inclusive():
    rows = _plan({M.RECORD_DATE: date(2026, 5, 8), M.LISTING: date(2026, 5, 31)})
    sev = {r.milestone: r.severity for r in rows}
    assert sev[M.RECORD_DATE] == "critical"  # exactly 7
    assert sev[M.LISTING] == "high"  # exactly 30


def test_plan_past_and_not_held_are_low():
    past = _plan({M.BOARD: date(2026, 4, 1)})
    assert past[0].severity == "low"
    watch = _plan({M.LISTING: date(2026, 5, 2)}, held=False)
    assert watch[0].severity == "low"


def test_plan_today_is_not_past():
    rows = _plan({M.LISTING: date(2026, 5, 1)})
    assert rows[0].days_until == 0
    assert rows[0].reached is False
    assert rows[0].severity == "critical"


def test_plan_flags_out_of_order_dates():
    rows = _plan({M.BOARD: date(2026, 3, 1), M.SHAREHOLDER: date(2026, 2, 1), M.LISTING: date(2026, 7, 1)})
    flags = {r.milestone: r.out_of_order for r in rows}
    assert flags == {M.BOARD: False, M.SHAREHOLDER: True, M.LISTING: False}


def test_plan_same_day_is_not_out_of_order():
    rows = _plan({M.BOARD: date(2026, 3, 1), M.SHAREHOLDER: date(2026, 3, 1)})
    assert not any(r.out_of_order for r in rows)


def test_plan_empty():
    assert _plan({}) == []


def test_thresholds_required_and_ordered():
    with pytest.raises(ValueError):
        _plan({M.LISTING: date(2026, 6, 1)}, critical_within_days=30, high_within_days=7)
    with pytest.raises(TypeError):
        cal.plan_demerger_chain({}, today=date(2026, 5, 1), held=True)  # no invented defaults


@pytest.mark.parametrize(
    "basis,adjusts",
    [("exchange_field", True), ("human_verified", True), ("unverified", False)],
)
def test_entitlement_adjusts_price_only_when_verified(basis, adjusts):
    assert cal.entitlement_adjusts_price(basis) is adjusts


def test_entitlement_unknown_basis_adjusts_nothing():
    assert cal.entitlement_adjusts_price("whatever") is False
