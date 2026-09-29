"""Order-win facts from NSE announcements: value, counterparty, period, intensity (Session 7e)."""

from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal

import pytest

from core.compute.order_wins import (
    Announcement,
    LookAhead,
    OrderIntensity,
    OrderWin,
    Quarantined,
    QuarantineReason,
    RevenueQuarter,
    RULE_VERSION,
    extract_order_win,
    order_intensity,
    trailing_revenue,
)
from core.timezones import IST

D = Decimal
CR = D("10000000")
ISIN = "INE002A01018"
ANNOUNCED = datetime(2026, 5, 4, 10, 30, tzinfo=IST)
T = datetime(2026, 5, 5, 9, 0, tzinfo=IST)


def ann(subject: str, description: str | None = None, **kw) -> Announcement:
    base = dict(
        isin=ISIN, subject=subject, description=description,
        announced_on=ANNOUNCED, as_of=ANNOUNCED, source_url="https://nse.example/ann/1",
    )
    base.update(kw)
    return Announcement(**base)


GOOD = ann(
    "Receipt of Order",
    "The Company has received an order from Indian Railways for supply of 40 "
    "locomotives worth Rs. 2,500 crore, to be executed within 24 months.",
)


def test_rule_version():
    assert RULE_VERSION == "order_wins/1"


def test_extracts_value_as_decimal_rupees():
    w = extract_order_win(GOOD, T)
    assert isinstance(w, OrderWin)
    assert w.order_value_inr == D("2500") * CR
    assert isinstance(w.order_value_inr, Decimal)
    assert "2,500 crore" in w.value_quote


def test_extracts_counterparty_verbatim():
    w = extract_order_win(GOOD, T)
    assert w.counterparty == "Indian Railways"
    assert w.counterparty in GOOD.description


def test_extracts_execution_period_in_months():
    w = extract_order_win(GOOD, T)
    assert w.execution_months == D("24")
    assert "24 months" in w.period_quote


def test_years_convert_to_months():
    w = extract_order_win(
        ann("Order", "Received an order from NHAI worth Rs 900 crore, completion over 3 years."), T
    )
    assert w.execution_months == D("36")


def test_execution_end_date_gives_months_by_day_arithmetic():
    w = extract_order_win(
        ann("Order", "Received an order from NTPC worth Rs 100 crore, to be completed by 04-May-2027."), T
    )
    assert w.execution_end == date(2027, 5, 4)
    assert w.execution_months == D("365") / D("30.4375")


def test_execution_end_before_announcement_is_not_a_period():
    w = extract_order_win(
        ann("Order", "Received an order from NTPC worth Rs 100 crore, to be completed by 04-May-2025."), T
    )
    assert isinstance(w, OrderWin)
    assert w.execution_months is None and w.execution_end is None
    assert "execution_period" in w.missing


def test_missing_fields_are_named_not_guessed():
    w = extract_order_win(ann("Order win", "The Company has won an order worth Rs 50 crore."), T)
    assert isinstance(w, OrderWin)
    assert w.counterparty is None and w.execution_months is None
    assert set(w.missing) == {"counterparty", "execution_period"}


def test_provenance_carried():
    w = extract_order_win(GOOD, T)
    assert w.isin == ISIN
    assert w.as_of == ANNOUNCED and w.announced_on == ANNOUNCED
    assert w.source_url == "https://nse.example/ann/1"
    assert w.rule_version == RULE_VERSION
    assert w.matched_phrase


def test_no_value_is_quarantined():
    q = extract_order_win(ann("Receipt of order", "Received an order from BHEL."), T)
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.NO_VALUE


def test_two_different_values_are_quarantined_not_picked():
    q = extract_order_win(
        ann("Order", "Received an order from BHEL worth Rs 100 crore, total book Rs 900 crore."), T
    )
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.MULTIPLE_VALUES


def test_same_value_written_twice_is_one_value():
    w = extract_order_win(
        ann("Order", "Received an order from BHEL worth Rs 100 crore (INR 100 Cr)."), T
    )
    assert isinstance(w, OrderWin)
    assert w.order_value_inr == D("100") * CR


def test_tax_order_is_quarantined_as_excluded():
    q = extract_order_win(
        ann("Order", "Received assessment order from Income Tax department Rs 100 crore."), T
    )
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.EXCLUDED


def test_award_plus_exclusion_is_ambiguous():
    q = extract_order_win(
        ann("Order", "Received an order for Rs 100 crore; separately a tax tribunal order."), T
    )
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.AMBIGUOUS


def test_lowest_bidder_is_pre_award_never_a_win():
    q = extract_order_win(ann("L1", "Emerged as L1 bidder for Rs 400 crore project by NHAI."), T)
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.PRE_AWARD


def test_unrelated_announcement_is_not_an_order():
    q = extract_order_win(ann("Board meeting", "Board meeting on 15-Feb-2026."), T)
    assert isinstance(q, Quarantined)
    assert q.reason is QuarantineReason.NOT_AN_ORDER


def test_boilerplate_does_not_exclude():
    w = extract_order_win(
        ann(
            "Order",
            "Pursuant to Regulation 30 of SEBI (Listing Obligations and Disclosure Requirements) "
            "Regulations, 2015, received an order from GAIL worth Rs 75 crore.",
        ),
        T,
    )
    assert isinstance(w, OrderWin)


def test_refuses_announcement_dated_after_t():
    late = ann("Order", "Received an order worth Rs 1 crore.", as_of=datetime(2026, 5, 6, tzinfo=IST))
    with pytest.raises(LookAhead):
        extract_order_win(late, T)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        extract_order_win(ann("Order", "x", as_of=datetime(2026, 5, 4)), T)


# --- trailing revenue and intensity ----------------------------------------


def q(start, end, rev_cr, as_of=None):
    return RevenueQuarter(
        period_start=start, period_end=end, revenue_inr=D(rev_cr) * CR,
        as_of=as_of or datetime(2026, 4, 30, tzinfo=IST),
    )


FOUR = [
    q(date(2025, 1, 1), date(2025, 3, 31), 1000),
    q(date(2025, 4, 1), date(2025, 6, 30), 1100),
    q(date(2025, 7, 1), date(2025, 9, 30), 1200),
    q(date(2025, 10, 1), date(2025, 12, 31), 1300),
]


def test_trailing_revenue_sums_last_four_contiguous_quarters():
    tr = trailing_revenue(FOUR, T)
    assert tr.revenue_inr == D("4600") * CR
    assert tr.period_end == date(2025, 12, 31)
    assert tr.as_of == datetime(2026, 4, 30, tzinfo=IST)


def test_trailing_revenue_uses_latest_four_of_many():
    older = q(date(2024, 10, 1), date(2024, 12, 31), 9999)
    assert trailing_revenue([older, *FOUR], T).revenue_inr == D("4600") * CR


def test_trailing_revenue_none_with_fewer_than_four():
    assert trailing_revenue(FOUR[1:], T) is None


def test_trailing_revenue_none_with_a_gap():
    gap = [FOUR[0], FOUR[1], FOUR[3], q(date(2026, 1, 1), date(2026, 3, 31), 1)]
    assert trailing_revenue(gap, T) is None


def test_trailing_revenue_refuses_look_ahead():
    late = [*FOUR[:3], q(date(2025, 10, 1), date(2025, 12, 31), 1300, as_of=datetime(2026, 6, 1, tzinfo=IST))]
    with pytest.raises(LookAhead):
        trailing_revenue(late, T)


def test_intensity_is_arithmetic_percent_of_trailing_revenue():
    w = extract_order_win(GOOD, T)
    i = order_intensity(w, trailing_revenue(FOUR, T), T)
    assert isinstance(i, OrderIntensity)
    assert i.pct_of_trailing_revenue == D("2500") / D("4600") * 100
    # 24 months: two years, so annualised is half.
    assert i.annualised_pct == D("2500") / D("4600") * 100 / 2


def test_annualised_absent_without_period_and_says_why():
    w = extract_order_win(ann("Order win", "Won an order from GAIL worth Rs 460 crore."), T)
    i = order_intensity(w, trailing_revenue(FOUR, T), T)
    assert i.pct_of_trailing_revenue == D("10")
    assert i.annualised_pct is None
    assert i.gaps == ("execution_period",)


def test_no_revenue_gives_no_intensity_and_says_why():
    w = extract_order_win(GOOD, T)
    i = order_intensity(w, None, T)
    assert i.pct_of_trailing_revenue is None and i.annualised_pct is None
    assert "trailing_revenue" in i.gaps


def test_nonpositive_revenue_gives_no_intensity():
    w = extract_order_win(GOOD, T)
    zero = [
        q(date(2025, 1, 1), date(2025, 3, 31), 0),
        q(date(2025, 4, 1), date(2025, 6, 30), 0),
        q(date(2025, 7, 1), date(2025, 9, 30), 0),
        q(date(2025, 10, 1), date(2025, 12, 31), 0),
    ]
    i = order_intensity(w, trailing_revenue(zero, T), T)
    assert i.pct_of_trailing_revenue is None
    assert "trailing_revenue" in i.gaps


def test_intensity_refuses_win_dated_after_t():
    w = extract_order_win(GOOD, T)
    with pytest.raises(LookAhead):
        order_intensity(w, trailing_revenue(FOUR, T), datetime(2026, 5, 1, tzinfo=IST))


def test_intensity_carries_inputs_and_rule_version():
    w = extract_order_win(GOOD, T)
    tr = trailing_revenue(FOUR, T)
    i = order_intensity(w, tr, T)
    assert i.order_value_inr == w.order_value_inr
    assert i.trailing_revenue_inr == tr.revenue_inr
    assert i.rule_version == RULE_VERSION


def test_no_verdict_or_action_fields():
    for cls in (OrderWin, OrderIntensity, Quarantined):
        names = {f.name.lower() for f in fields(cls)}
        assert not names & {"action", "verdict", "signal", "recommendation", "score"}
