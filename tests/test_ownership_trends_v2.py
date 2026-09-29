"""ownership_trends/2 policy: cumulative promoter sales over a window, any pledge rise, explanation label.

Reductions of the promoter percentage from dilution are not sales: only shares actually sold count.
The module never decides whether a management update mentions a pledge and never clears a flag.
"""

import inspect
from datetime import date
from decimal import Decimal

import pytest

from core.compute import ownership_trends as ot
from core.compute.ownership_rules import InsiderTradeInput, NotEvaluableResult
from core.compute.ownership_trends import (
    PLEDGE_ANY_RISE_PP,
    HoldingInput,
    ManagementUpdateInput,
    PatternFactInput,
    pledged_pct_change_facts,
    promoter_sale_pct_of_stake_facts,
)

D = Decimal
ISIN = "INE000A01010"
Q = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]
HELD = (HoldingInput(isin=ISIN, quantity=D("100")),)
AS_OF = date(2026, 3, 31)


def trade(day, qty, mode="open_market"):
    return InsiderTradeInput(
        isin=ISIN, person_name="P", person_category="Promoter", acquisition_mode=mode,
        trade_date=day, quantity=D(qty), price_per_share=D("10"), post_trade_holding_pct=None,
    )  # fmt: skip


def fact(day, category, measure, value):
    return PatternFactInput(as_on_date=day, category=category, measure=measure, value=D(value))


def promoter(day, total, pledged):
    return [
        fact(day, "promoter_group", "total_shares", total),
        fact(day, "promoter_group", "pledged_shares", pledged),
    ]


def update(day, mentions=True, kind="CONCALL", isin=ISIN):
    return ManagementUpdateInput(
        isin=isin, source_kind=kind, as_of=day, mentions_pledge=mentions,
        url="https://example.test/u", provenance="caller supplied",
    )  # fmt: skip


class TestCumulativeSale:
    stake = promoter(date(2025, 12, 31), 10000, 0)

    def run(self, trades, x="1", window=365, as_of=AS_OF, holdings=HELD, facts=None):
        return promoter_sale_pct_of_stake_facts(
            trades, self.stake if facts is None else facts, isin=ISIN, x_pct=D(x),
            holdings=holdings, as_of_date=as_of, window_days=window,
        )  # fmt: skip

    def test_small_sales_sum_past_threshold_though_each_is_below(self):
        trades = [trade(date(2026, 1, 10), "-60"), trade(date(2026, 2, 10), "-60")]  # 0.6% + 0.6%
        out = self.run(trades)
        assert len(out) == 1
        f = out[0]
        assert f.value == D("1.2") and f.severity == "critical"
        assert f.flags == ("CRITICAL", "EXIT_REVIEW") and f.event_date == AS_OF
        assert len([e for e in f.evidence if e.startswith("insider_trade:")]) == 2

    def test_exactly_at_threshold_is_not_a_flag(self):
        assert self.run([trade(date(2026, 1, 10), "-100")]) == []

    def test_non_holding_is_high_without_flags(self):
        f = self.run([trade(date(2026, 1, 10), "-150")], holdings=())[0]
        assert f.severity == "high" and f.flags == ()

    def test_sales_outside_window_or_after_as_of_do_not_count(self):
        trades = [
            trade(date(2025, 1, 10), "-80"),
            trade(date(2026, 1, 10), "-80"),
            trade(date(2026, 4, 10), "-80"),
        ]
        assert self.run(trades) == []

    def test_dilution_only_fall_produces_no_flag(self):
        # promoter share count unchanged, company issued shares: percentage fell, nothing was sold
        facts = promoter(Q[3], 10000, 0) + promoter(Q[4], 10000, 0)
        assert self.run([], facts=facts) == []

    def test_dilution_alongside_small_sale_counts_only_shares_sold(self):
        # 50 shares sold = 0.5% of stake; any dilution fall elsewhere is invisible to the rule
        assert self.run([trade(date(2026, 1, 10), "-50")]) == []

    def test_buys_and_non_open_market_never_count(self):
        trades = [trade(date(2026, 1, 10), "500"), trade(date(2026, 1, 11), "-500", mode="gift")]
        assert self.run(trades) == []

    def test_sale_without_known_stake_is_not_evaluable(self):
        out = self.run([trade(date(2026, 1, 10), "-500")], facts=promoter(Q[4], 10000, 0))
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)

    def test_parameters_required_and_non_positive_rejected(self):
        params = inspect.signature(promoter_sale_pct_of_stake_facts).parameters
        for n in ("x_pct", "window_days", "as_of_date"):
            assert params[n].default is inspect.Parameter.empty
        with pytest.raises(ValueError):
            self.run([], window=0)
        with pytest.raises(ValueError):
            self.run([], x="0")


class TestPledgeAnyRise:
    facts_rise = promoter(Q[0], 1000, 100) + promoter(Q[1], 1000, 101)  # +0.1 point

    def run(self, facts, updates=(), holdings=HELD, y=PLEDGE_ANY_RISE_PP, as_of=AS_OF):
        return pledged_pct_change_facts(
            facts, isin=ISIN, y_points=y, holdings=holdings, as_of_date=as_of,
            management_updates=updates,
        )  # fmt: skip

    def test_constant_value(self):
        assert PLEDGE_ANY_RISE_PP == D("0.01")

    def test_tiny_rise_on_holding_is_critical_with_no_explanation(self):
        f = self.run(self.facts_rise)[0]
        assert f.severity == "critical" and f.flags == ("CRITICAL", "EXIT_REVIEW")
        assert f.explanation_status == "NO_EXPLANATION_FOUND"

    def test_explanation_labels_but_never_clears_or_downgrades(self):
        f = self.run(self.facts_rise, [update(date(2025, 8, 1))])[0]
        assert f.explanation_status == "EXPLAINED_BY_MANAGEMENT_UPDATE"
        assert f.severity == "critical" and f.flags == ("CRITICAL", "EXIT_REVIEW")
        assert any(e.startswith("management_update:") and "example.test" in e for e in f.evidence)

    def test_update_on_period_end_counts_before_it_does_not(self):
        on_end = self.run(self.facts_rise, [update(Q[1])])[0]
        assert on_end.explanation_status == "EXPLAINED_BY_MANAGEMENT_UPDATE"
        before = self.run(self.facts_rise, [update(date(2025, 6, 29))])[0]
        assert before.explanation_status == "NO_EXPLANATION_FOUND"

    def test_update_after_as_of_is_look_ahead_and_ignored(self):
        f = self.run(self.facts_rise, [update(date(2025, 8, 1))], as_of=date(2025, 7, 15))[0]
        assert f.explanation_status == "NO_EXPLANATION_FOUND"

    def test_update_not_mentioning_pledge_or_other_isin_ignored(self):
        ups = [update(date(2025, 8, 1), mentions=False), update(date(2025, 8, 1), isin="INE000B01019")]
        assert self.run(self.facts_rise, ups)[0].explanation_status == "NO_EXPLANATION_FOUND"

    def test_each_source_kind_accepted_and_unknown_rejected(self):
        for kind in ("CONCALL", "AGM", "SGM", "ANNUAL_REPORT", "ANNUAL_REVIEW"):
            f = self.run(self.facts_rise, [update(date(2025, 8, 1), kind=kind)])[0]
            assert f.explanation_status == "EXPLAINED_BY_MANAGEMENT_UPDATE"
        with pytest.raises(ValueError):
            self.run(self.facts_rise, [update(date(2025, 8, 1), kind="TWEET")])

    def test_non_holding_still_flagged_high_with_status(self):
        f = self.run(self.facts_rise, holdings=())[0]
        assert f.severity == "high" and f.flags == ()
        assert f.explanation_status == "NO_EXPLANATION_FOUND"

    def test_pattern_facts_after_as_of_are_not_used(self):
        out = self.run(self.facts_rise, as_of=date(2025, 6, 1))
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)

    def test_fall_or_flat_no_fact_and_new_arguments_required(self):
        assert self.run(promoter(Q[0], 1000, 200) + promoter(Q[1], 1000, 200)) == []
        params = inspect.signature(pledged_pct_change_facts).parameters
        assert params["management_updates"].default is inspect.Parameter.empty
        assert params["as_of_date"].default is inspect.Parameter.empty

    def test_other_facts_default_to_no_explanation_status(self):
        assert ot.OwnershipFact.__dataclass_fields__["explanation_status"].default is None
