"""The Kite-ATH-Scanner fixtures, asserted rather than described.

tests/fixtures/kite_ath_scanner/SOURCES.md states three things these files are
evidence of. Prose drifts from data silently, so each claim is checked here: if
a fixture is ever regenerated and the claim stops holding, that is a finding
about the vendor or the exchange, not a test to relax.
"""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "kite_ath_scanner"

# Known NSE capital actions, with the close-to-close step an UNADJUSTED series
# would have to show on the ex-date.
KNOWN_ACTIONS = [
    ("RELIANCE", "2017-09-07", "1:1 bonus", Decimal("0.5")),
    ("INFY", "2018-09-11", "1:1 bonus", Decimal("0.5")),
    ("INFY", "2015-06-15", "1:1 bonus", Decimal("0.5")),
    ("TITAN", "2011-09-05", "1:10 split", Decimal("0.1")),
    ("TCS", "2018-05-31", "1:1 bonus", Decimal("0.5")),
]

# A real step lands within 15% of the ratio; anything near 1.0 means the vendor
# already divided the earlier prices.
STEP_TOLERANCE = Decimal("0.15")
NO_STEP_BAND = (Decimal("0.85"), Decimal("1.15"))


def _candles() -> dict[str, dict[str, Decimal]]:
    """{symbol: {date: close}} from the corporate-action window fixture."""
    out: dict[str, dict[str, Decimal]] = {}
    with (FIXTURES / "candles_corporate_action_windows.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["symbol"], {})[row["date"]] = Decimal(row["close"])
    return out


@pytest.mark.parametrize(("symbol", "ex_date", "action", "step"), KNOWN_ACTIONS)
def test_vendor_history_is_back_adjusted(symbol, ex_date, action, step):
    """Upstox's daily candles carry adjustments for actions after the trade date.

    This is why ingest/upstox/adapters.py sets `as_of` to the fetch time and why
    core/compute/adjustment.py recomputes factors from `corporate_action` rows
    filtered by `price_adjustments_as_of` (R2). A series that already knows
    about a later split is look-ahead contaminated at every earlier date.
    """
    closes = _candles()[symbol]
    before = [d for d in sorted(closes) if d < ex_date]
    on_or_after = [d for d in sorted(closes) if d >= ex_date]
    assert before and on_or_after, f"fixture lacks a window around {ex_date}"

    ratio = closes[on_or_after[0]] / closes[before[-1]]

    assert not abs(ratio - step) < step * STEP_TOLERANCE, (
        f"{symbol} shows the {action} step at {ex_date} (ratio {ratio:.3f}); the "
        f"series is as-traded, not adjusted. SOURCES.md and the adapter's "
        f"`as_of` reasoning both assume otherwise."
    )
    low, high = NO_STEP_BAND
    assert low < ratio < high, (
        f"{symbol} moved {ratio:.3f} across {ex_date} — neither the {action} "
        f"step nor an ordinary day. Check the ex-date before trusting either."
    )


def test_reliance_year_2000_close_is_deeply_back_adjusted():
    """The stored 2000 close is ~10x below the as-traded price of the time.

    A smoking gun rather than a threshold: NSE's as-traded RELIANCE close in
    January 2000 was around Rs 250, so a stored 23.10 is a retroactive
    adjustment applied to prices 26 years old.
    """
    closes = _candles()["RELIANCE"]
    first = min(closes)
    assert first.startswith("2000-01"), first
    assert closes[first] < Decimal("50"), (
        f"RELIANCE {first} close {closes[first]} is no longer far below the "
        f"as-traded ~Rs 250; the fixture may have been regenerated unadjusted."
    )


def test_desc_is_a_controlled_vocabulary_with_an_inverted_sibling():
    """`desc` is a dropdown, and one order-shaped category means the opposite.

    `ACTION(S) TAKEN OR ORDERS PASSED` is a regulator or court acting against
    the company. It is nearly as common as genuine order wins, so matching the
    word "order" without an exclusion set makes the category roughly half noise
    of inverted sign. Bears on core/compute/order_terms.py.
    """
    with (FIXTURES / "nse_announcement_categories.csv").open(newline="", encoding="utf-8") as f:
        counts = {r["desc_category"]: int(r["row_count"]) for r in csv.DictReader(f)}

    # A dropdown, not free text: 391k filings over 2+ years fit in a small set.
    assert 100 < len(counts) < 400, len(counts)
    assert sum(counts.values()) == 391_074

    wins = counts["BAGGING/RECEIVING OF ORDERS/CONTRACTS"] + counts["AWARDING OF ORDER(S)/CONTRACT(S)"]
    against = counts["ACTION(S) TAKEN OR ORDERS PASSED"]
    assert against > wins * 0.5, (
        f"{against} regulatory-action rows against {wins} order wins: the "
        f"exclusion set is load-bearing, not a refinement."
    )

    # The recorded false positive: phrase matching on `desc` made "Trading
    # Window" an order win via "WIN", across this many rows.
    assert counts["TRADING WINDOW"] > 20_000


def test_announcement_sample_keeps_the_attachment_pointer():
    """The order value is in the PDF, not in the snippet.

    `attchmnt_text` is truncated (median ~100 chars; ~9.5% carry a money
    figure), so any parser that reads values must follow `attchmnt_file`.
    `att_file_size` is what lets a fetch skip an outsized PDF first.
    """
    rows = json.loads((FIXTURES / "nse_announcements_sample.json").read_text(encoding="utf-8"))
    assert len(rows) == 60

    required = {"isin", "symbol", "an_dt", "desc", "attchmnt_text", "sort_date",
                "seq_id", "attchmnt_file", "att_file_size"}
    for row in rows:
        assert required <= set(row), sorted(required - set(row))
        assert row["isin"].startswith("INE"), row["isin"]
        assert row["attchmnt_file"].startswith("http"), row["attchmnt_file"]

    # Six categories, ten rows each.
    by_desc: dict[str, int] = {}
    for row in rows:
        by_desc[row["desc"].upper().strip()] = by_desc.get(row["desc"].upper().strip(), 0) + 1
    assert sorted(by_desc.values()) == [10] * 6, by_desc
