"""Ownership rules, remainder of CLAUDE.md: windows, quarterly trends, pledges, clusters, passive tags.

Pure functions; holdings are a plain input type. X, Y, N, window and cluster size are required
caller parameters and never defaulted. Outputs are facts with rule_version and evidence, never verdicts.
"""

import inspect
from datetime import date
from decimal import Decimal

import pytest

from core.compute import ownership_trends as ot
from core.compute.ownership_rules import InsiderTradeInput, NotEvaluableResult, StakeDisclosureInput
from core.compute.ownership_trends import (
    RULE_VERSION,
    HoldingInput,
    IndexEventInput,
    OwnershipFact,
    PatternFactInput,
    fii_dii_trend,
    passive_inflow_periods,
    pledge_event_facts,
    pledged_pct_change_facts,
    promoter_open_market_flow,
    promoter_sale_pct_of_stake_facts,
    promoter_selling_before_results,
)

D = Decimal
ISIN = "INE000A01010"
Q = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]
HELD = (HoldingInput(isin=ISIN, quantity=D("100")),)


def trade(day, qty, mode="open_market", cat="Promoter", isin=ISIN):
    return InsiderTradeInput(
        isin=isin, person_name="P", person_category=cat, acquisition_mode=mode,
        trade_date=day, quantity=D(qty), price_per_share=D("10"), post_trade_holding_pct=None,
    )  # fmt: skip


def fact(day, category, measure, value):
    return PatternFactInput(as_on_date=day, category=category, measure=measure, value=D(value))


def inst(day, foreign, domestic):
    return [
        fact(day, "institutions_foreign", "total_shares", foreign),
        fact(day, "institutions_domestic", "total_shares", domestic),
    ]


def promoter(day, total, pledged):
    return [
        fact(day, "promoter_group", "total_shares", total),
        fact(day, "promoter_group", "pledged_shares", pledged),
    ]


def disc(kind, day=date(2026, 1, 10)):
    return StakeDisclosureInput(
        isin=ISIN, acquirer_name="Promoter Co", disclosure_type=kind, disclosure_date=day,
        shares_acquired=D("500"), post_acquisition_pct=None,
    )  # fmt: skip


class TestContract:
    def test_rule_version_and_frozen_fact(self):
        assert RULE_VERSION == "ownership_trends/1"
        f = OwnershipFact(
            isin=ISIN, fact_type="x", direction="inflow", severity="low", event_date=Q[0],
            value=None, detail="d", flags=(), evidence=("e",), rule_version=RULE_VERSION,
        )  # fmt: skip
        with pytest.raises(AttributeError):
            f.detail = "y"

    def test_thresholds_are_required_never_defaulted(self):
        for fn, names in [
            (promoter_open_market_flow, {"window_days", "inflow_threshold_quantity"}),
            (fii_dii_trend, {"n_quarters"}),
            (pledged_pct_change_facts, {"y_points"}),
            (promoter_sale_pct_of_stake_facts, {"x_pct"}),
            (promoter_selling_before_results, {"window_days", "min_trades"}),
        ]:
            params = inspect.signature(fn).parameters
            for n in names:
                assert params[n].default is inspect.Parameter.empty, (fn.__name__, n)

    def test_non_positive_parameters_rejected(self):
        with pytest.raises(ValueError):
            fii_dii_trend([], isin=ISIN, n_quarters=0, passive_periods=frozenset())
        with pytest.raises(ValueError):
            promoter_open_market_flow(
                [], isin=ISIN, as_of_date=Q[0], window_days=0, inflow_threshold_quantity=D("1")
            )


class TestPromoterOpenMarketFlow:
    def run(self, trades, thr="100", window=30):
        return promoter_open_market_flow(
            trades, isin=ISIN, as_of_date=date(2026, 2, 1), window_days=window,
            inflow_threshold_quantity=D(thr),
        )  # fmt: skip

    def test_net_buying_above_threshold_is_positive_inflow(self):
        out = self.run([trade(date(2026, 1, 20), "80"), trade(date(2026, 1, 25), "50")])
        assert len(out) == 1
        f = out[0]
        assert (f.fact_type, f.direction, f.value) == ("promoter_open_market_buying", "inflow", D("130"))
        assert f.rule_version == RULE_VERSION and len(f.evidence) == 2

    def test_buying_at_or_below_threshold_is_no_fact(self):
        assert self.run([trade(date(2026, 1, 20), "100")]) == []

    def test_net_selling_is_negative_outflow_without_threshold(self):
        out = self.run([trade(date(2026, 1, 20), "-5")])
        assert [(f.fact_type, f.direction, f.value) for f in out] == [
            ("promoter_open_market_selling", "outflow", D("-5"))
        ]

    def test_outside_window_and_false_signals_and_non_promoters_ignored(self):
        out = self.run(
            [
                trade(date(2025, 12, 1), "1000"),  # before window
                trade(date(2026, 1, 20), "1000", mode="gift"),
                trade(date(2026, 1, 20), "1000", mode="esos_esop"),
                trade(date(2026, 1, 20), "1000", mode="off_market"),  # unclassified, not read as a signal
                trade(date(2026, 1, 20), "1000", cat="Director"),
                trade(date(2026, 1, 20), "1000", isin="INE000B01019"),
                trade(date(2026, 2, 2), "1000"),  # after as_of_date
            ]
        )
        assert out == []

    def test_window_boundary_is_exclusive_at_start(self):
        # window 30 days ending 2026-02-01 covers 2026-01-03 .. 2026-02-01
        assert len(self.run([trade(date(2026, 1, 3), "500")])) == 1
        assert self.run([trade(date(2026, 1, 2), "500")]) == []


class TestFiiDiiTrend:
    def series(self, pairs):
        out = []
        for day, (fo, do) in zip(Q, pairs):
            out += inst(day, fo, do)
        return out

    def run(self, facts, n=2, passive=frozenset()):
        return fii_dii_trend(facts, isin=ISIN, n_quarters=n, passive_periods=passive)

    def test_rising_for_n_consecutive_quarters(self):
        out = self.run(self.series([(100, 100), (110, 100), (110, 120)]))
        assert [f.fact_type for f in out] == ["fii_dii_rising"]
        assert out[0].direction == "inflow" and out[0].value == D("30")
        assert out[0].event_date == Q[2] and out[0].rule_version == RULE_VERSION

    def test_falling_for_n_consecutive_quarters_is_exit(self):
        out = self.run(self.series([(100, 100), (90, 100), (90, 80)]))
        assert [(f.fact_type, f.direction, f.value) for f in out] == [
            ("fii_dii_exiting", "outflow", D("-30"))
        ]

    def test_uses_only_the_last_n_changes(self):
        out = self.run(self.series([(200, 0), (100, 0), (110, 0), (120, 0)]), n=2)
        assert [f.fact_type for f in out] == ["fii_dii_rising"]

    def test_mixed_direction_is_no_fact(self):
        assert self.run(self.series([(100, 100), (110, 100), (105, 100)])) == []

    def test_flat_quarter_breaks_the_run(self):
        assert self.run(self.series([(100, 100), (100, 100), (110, 100)])) == []

    def test_too_few_quarters_is_not_evaluable(self):
        out = self.run(self.series([(100, 100), (110, 100)]))
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)
        assert "insufficient" in out[0].reason

    def test_gap_between_quarters_is_not_evaluable(self):
        facts = inst(Q[0], 100, 0) + inst(Q[2], 110, 0) + inst(Q[3], 120, 0)
        out = self.run(facts)
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)
        assert "consecutive" in out[0].reason

    def test_missing_category_is_not_evaluable(self):
        facts = [fact(q, "institutions_foreign", "total_shares", 1) for q in Q[:3]]
        out = self.run(facts)
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)

    def test_rise_in_passive_quarter_is_tagged_not_conviction(self):
        out = self.run(self.series([(100, 100), (110, 100), (110, 120)]), passive=frozenset({Q[2]}))
        assert [f.fact_type for f in out] == ["fii_dii_rise_passive_tagged"]
        assert out[0].severity == "low"


class TestPassiveInflowPeriods:
    def ev(self, day, kind="inclusion", status="effective", isin=ISIN):
        return IndexEventInput(
            isin=isin, index_code="nifty_50", event_type=kind, status=status, effective_date=day
        )  # fmt: skip

    def test_effective_inclusion_tags_its_quarter(self):
        out = passive_inflow_periods([self.ev(date(2025, 8, 15))], isin=ISIN, period_ends=Q)
        assert out == frozenset({Q[2]})

    def test_quarter_end_day_belongs_to_that_quarter(self):
        out = passive_inflow_periods([self.ev(date(2025, 9, 30))], isin=ISIN, period_ends=Q)
        assert out == frozenset({Q[2]})

    def test_only_effective_inclusions_of_this_isin(self):
        events = [
            self.ev(date(2025, 8, 15), status="announced"),
            self.ev(date(2025, 8, 15), kind="exclusion"),
            self.ev(date(2025, 8, 15), kind="weight_change"),
            self.ev(date(2025, 8, 15), isin="INE000B01019"),
            self.ev(None),
        ]
        assert passive_inflow_periods(events, isin=ISIN, period_ends=Q) == frozenset()


class TestPledgeEvents:
    def test_released_is_positive_created_is_negative(self):
        out = pledge_event_facts([disc("pledge_released"), disc("pledge_created")], isin=ISIN)
        got = {f.fact_type: (f.direction, f.severity) for f in out}
        assert got == {
            "pledge_released": ("inflow", "medium"),
            "pledge_created": ("outflow", "medium"),
        }
        assert all(f.rule_version == RULE_VERSION and f.evidence for f in out)

    def test_other_disclosure_types_and_isins_ignored(self):
        other = StakeDisclosureInput(
            isin="INE000B01019", acquirer_name="x", disclosure_type="pledge_created",
            disclosure_date=Q[0], shares_acquired=None, post_acquisition_pct=None,
        )  # fmt: skip
        assert pledge_event_facts([disc("sast_5pct"), disc("pledge_invoked"), other], isin=ISIN) == []


class TestPledgedPctChange:
    def run(self, facts, y="5", holdings=HELD):
        return pledged_pct_change_facts(facts, isin=ISIN, y_points=D(y), holdings=holdings)

    def test_rise_above_y_for_a_holding_is_critical_with_exit_review(self):
        facts = promoter(Q[0], 1000, 100) + promoter(Q[1], 1000, 200)  # 10% -> 20%
        out = self.run(facts)
        assert len(out) == 1
        f = out[0]
        assert f.fact_type == "pledged_pct_rising" and f.value == D("10")
        assert f.severity == "critical" and f.flags == ("CRITICAL", "EXIT_REVIEW")

    def test_rise_above_y_for_a_non_holding_is_high_without_flags(self):
        facts = promoter(Q[0], 1000, 100) + promoter(Q[1], 1000, 200)
        f = self.run(facts, holdings=())[0]
        assert f.severity == "high" and f.flags == ()

    def test_rise_at_or_below_y_is_negative_but_not_critical(self):
        facts = promoter(Q[0], 1000, 100) + promoter(Q[1], 1000, 150)  # +5 points, Y = 5
        f = self.run(facts)[0]
        assert f.severity == "medium" and f.flags == () and f.direction == "outflow"

    def test_fall_or_flat_is_no_fact(self):
        assert self.run(promoter(Q[0], 1000, 200) + promoter(Q[1], 1000, 100)) == []
        assert self.run(promoter(Q[0], 1000, 200) + promoter(Q[1], 1000, 200)) == []

    def test_uses_percent_of_promoter_shares_not_raw_counts(self):
        facts = promoter(Q[0], 1000, 100) + promoter(Q[1], 2000, 200)  # 10% -> 10%
        assert self.run(facts) == []

    def test_non_consecutive_quarters_and_missing_pledge_are_not_evaluable(self):
        gap = promoter(Q[0], 1000, 100) + promoter(Q[2], 1000, 900)
        out = self.run(gap)
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)
        nopledge = [
            fact(Q[0], "promoter_group", "total_shares", 10),
            fact(Q[1], "promoter_group", "total_shares", 10),
        ]
        out = self.run(nopledge)
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)


class TestPromoterSalePctOfStake:
    def run(self, trades, facts, x="10", holdings=HELD):
        return promoter_sale_pct_of_stake_facts(
            trades, facts, isin=ISIN, x_pct=D(x), holdings=holdings
        )  # fmt: skip

    stake = promoter(date(2025, 12, 31), 1000, 0)

    def test_sale_above_x_of_stake_for_holding_is_critical_exit_review(self):
        out = self.run([trade(date(2026, 1, 20), "-150")], self.stake)
        assert len(out) == 1
        f = out[0]
        assert f.fact_type == "promoter_sale_above_x" and f.value == D("15")
        assert f.severity == "critical" and f.flags == ("CRITICAL", "EXIT_REVIEW")

    def test_at_or_below_x_is_no_fact(self):
        assert self.run([trade(date(2026, 1, 20), "-100")], self.stake) == []

    def test_non_holding_gets_no_critical_flags(self):
        f = self.run([trade(date(2026, 1, 20), "-150")], self.stake, holdings=())[0]
        assert f.severity == "high" and f.flags == ()

    def test_only_promoter_open_market_sales_count(self):
        trades = [
            trade(date(2026, 1, 20), "-500", mode="gift"),
            trade(date(2026, 1, 20), "-500", cat="Director"),
            trade(date(2026, 1, 20), "500"),
        ]
        assert self.run(trades, self.stake) == []

    def test_stake_taken_from_latest_quarter_on_or_before_trade(self):
        facts = promoter(date(2025, 9, 30), 1000, 0) + promoter(date(2025, 12, 31), 2000, 0)
        # 150 of 2000 = 7.5%, below X = 10; a later quarter than the trade is never used
        facts += promoter(date(2026, 3, 31), 100, 0)
        assert self.run([trade(date(2026, 1, 20), "-150")], facts) == []

    def test_no_stake_known_is_not_evaluable(self):
        out = self.run([trade(date(2026, 1, 20), "-150")], promoter(date(2026, 3, 31), 1000, 0))
        assert len(out) == 1 and isinstance(out[0], NotEvaluableResult)


class TestSellingBeforeResults:
    def run(self, trades, results, window=10, min_trades=2):
        return promoter_selling_before_results(
            trades, results, isin=ISIN, window_days=window, min_trades=min_trades
        )  # fmt: skip

    def test_cluster_of_sales_shortly_before_results_is_negative(self):
        sales = [trade(date(2026, 1, 25), "-10"), trade(date(2026, 1, 28), "-20")]
        out = self.run(sales, [date(2026, 2, 3)])
        assert len(out) == 1
        f = out[0]
        assert f.fact_type == "promoter_selling_before_results" and f.direction == "outflow"
        assert f.value == D("-30") and f.event_date == date(2026, 2, 3) and len(f.evidence) == 2

    def test_below_min_trades_or_outside_window_or_after_results_is_no_fact(self):
        one = [trade(date(2026, 1, 28), "-10")]
        assert self.run(one, [date(2026, 2, 3)]) == []
        far = [trade(date(2026, 1, 1), "-10"), trade(date(2026, 1, 2), "-10")]
        assert self.run(far, [date(2026, 2, 3)]) == []
        after = [trade(date(2026, 2, 4), "-10"), trade(date(2026, 2, 5), "-10")]
        assert self.run(after, [date(2026, 2, 3)]) == []

    def test_purchases_and_false_signals_never_count(self):
        mixed = [trade(date(2026, 1, 28), "10"), trade(date(2026, 1, 29), "-10", mode="gift")]
        assert self.run(mixed, [date(2026, 2, 3)]) == []


class TestNoVerdicts:
    def test_module_public_surface_has_no_verdict_words(self):
        words = {"buy", "sell", "hold"}
        for name in dir(ot):
            assert not (words & set(name.lower().split("_"))), name
