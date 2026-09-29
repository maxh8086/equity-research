"""holding_weight_facts: value and weight of total, Decimal arithmetic, facts only. No database."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.holding_weights import HoldingInput, holding_weight_facts

A, B, C = "INE000A01004", "INE000B01002", "INE000C01000"


def h(isin: str, quantity: int, price: str) -> HoldingInput:
    return HoldingInput(isin=isin, quantity=quantity, last_price=Decimal(price))


def test_value_is_quantity_times_price_and_weight_is_share_of_total():
    facts = holding_weight_facts([h(A, 100, "300.25"), h(B, 50, "1300.10"), h(C, 10, "75.75")])
    assert facts.total_value == Decimal("30025.00") + Decimal("65005.00") + Decimal("757.50")
    by = {f.isin: f for f in facts.holdings}
    assert by[A].value == Decimal("30025.00")
    assert by[A].weight == Decimal("30025.00") / Decimal("95787.50")
    assert by[B].quantity == 50


def test_two_rows_of_one_isin_are_one_holding():
    facts = holding_weight_facts([h(A, 100, "300"), h(A, 20, "301"), h(B, 10, "100")])
    by = {f.isin: f for f in facts.holdings}
    assert len(facts.holdings) == 2
    assert by[A].quantity == 120
    assert by[A].value == Decimal("30000") + Decimal("6020")


def test_result_is_ordered_by_isin():
    facts = holding_weight_facts([h(C, 1, "1"), h(A, 1, "1"), h(B, 1, "1")])
    assert [f.isin for f in facts.holdings] == [A, B, C]


def test_no_float_anywhere():
    facts = holding_weight_facts([h(A, 3, "0.1"), h(B, 7, "0.2")])
    assert facts.total_value == Decimal("1.7")
    assert all(isinstance(f.value, Decimal) and isinstance(f.weight, Decimal) for f in facts.holdings)


def test_zero_total_has_no_weight_rather_than_a_division_error():
    facts = holding_weight_facts([h(A, 0, "300"), h(B, 5, "0")])
    assert facts.total_value == Decimal("0")
    assert [f.weight for f in facts.holdings] == [None, None]


def test_empty_input():
    facts = holding_weight_facts([])
    assert facts.total_value == Decimal("0")
    assert facts.holdings == ()


@pytest.mark.parametrize(
    "bad",
    [
        HoldingInput(isin=A, quantity=-1, last_price=Decimal("1")),
        HoldingInput(isin=A, quantity=1, last_price=Decimal("-1")),
        HoldingInput(isin=A, quantity=1, last_price=Decimal("NaN")),
        HoldingInput(isin=A, quantity=1.5, last_price=Decimal("1")),  # type: ignore[arg-type]
        HoldingInput(isin=A, quantity=True, last_price=Decimal("1")),  # type: ignore[arg-type]
        HoldingInput(isin=A, quantity=1, last_price=1.5),  # type: ignore[arg-type]
    ],
)
def test_invalid_inputs_are_refused(bad):
    with pytest.raises(ValueError):
        holding_weight_facts([bad])


positions = st.lists(
    st.tuples(
        st.sampled_from([A, B, C, "INE000D01008"]),
        st.integers(min_value=0, max_value=10**6),
        st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=2, allow_nan=False),
    ),
    max_size=12,
)


@given(positions)
def test_weights_sum_to_one_and_values_sum_to_total(rows):
    facts = holding_weight_facts([HoldingInput(i, q, p) for i, q, p in rows])
    assert sum((f.value for f in facts.holdings), Decimal("0")) == facts.total_value
    if facts.total_value > 0:
        assert abs(sum(f.weight for f in facts.holdings) - 1) < Decimal("1e-20")
    else:
        assert all(f.weight is None for f in facts.holdings)


@given(positions)
def test_input_order_does_not_change_the_facts(rows):
    forward = holding_weight_facts([HoldingInput(i, q, p) for i, q, p in rows])
    backward = holding_weight_facts([HoldingInput(i, q, p) for i, q, p in reversed(rows)])
    assert forward == backward
