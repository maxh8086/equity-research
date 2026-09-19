"""The validation sample: seeded draw, recorded swaps, and a frozen definition. No database."""

import hashlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.sample import InvalidSwap, Swap, apply_swaps, draw_key, seeded_draw
from core.db.models import IndexCode
from ingest.nse_indices.parser import parse_constituent_list
from validate.samples import SAMPLE_V1, SAMPLES

FIXTURES = Path(__file__).parent / "fixtures" / "nse_indices"
LISTS = {
    IndexCode.NIFTY_50: FIXTURES / "ind_nifty50list_20260915.csv",
    IndexCode.NIFTY_NEXT_50: FIXTURES / "ind_niftynext50list_20260915.csv",
}

isins = st.lists(st.from_regex(r"IN[A-Z0-9]{9}[0-9]", fullmatch=True), min_size=1, max_size=40, unique=True)


def _constituents(code: IndexCode) -> dict[str, str]:
    parsed = parse_constituent_list(LISTS[code].read_bytes(), code)
    return {c.row.isin: c.row.symbol for c in parsed.constituents}


# --------------------------------------------------------------------------- #
# core.compute.sample
# --------------------------------------------------------------------------- #


@given(isins, st.data())
def test_draw_ignores_population_order(population, data):
    n = data.draw(st.integers(1, len(population)))
    shuffled = data.draw(st.permutations(population))
    assert seeded_draw(population, seed="s", n=n) == seeded_draw(shuffled, seed="s", n=n)


@given(isins, st.data())
def test_draw_is_the_lowest_keys_and_nested(population, data):
    n = data.draw(st.integers(1, len(population)))
    drawn = seeded_draw(population, seed="s", n=n)
    assert len(set(drawn)) == n
    keys = [draw_key("s", i) for i in drawn]
    assert keys == sorted(keys)
    assert max(keys) < min((draw_key("s", i) for i in set(population) - set(drawn)), default="g")
    # Drawing fewer gives a prefix of drawing more.
    assert seeded_draw(population, seed="s", n=max(1, n - 1)) == drawn[: max(1, n - 1)]


def test_draw_rejects_bad_sizes_and_duplicates():
    with pytest.raises(ValueError):
        seeded_draw(["A", "A"], seed="s", n=1)
    with pytest.raises(ValueError):
        seeded_draw(["A"], seed="s", n=2)
    with pytest.raises(ValueError):
        seeded_draw(["A"], seed="s", n=0)


def test_draw_key_is_sha256_of_seed_and_isin():
    assert draw_key("seed", "INE002A01018") == hashlib.sha256(b"seed:INE002A01018").hexdigest()


def test_swaps_replace_in_place():
    assert apply_swaps(("A", "B", "C"), {"A", "B", "C", "D"}, [Swap("B", "D", "why")]) == ("A", "D", "C")


@pytest.mark.parametrize(
    "swap",
    [
        Swap("B", "D", " "),  # no reason
        Swap("X", "D", "why"),  # not in the sample
        Swap("B", "Z", "why"),  # not in the population
        Swap("B", "C", "why"),  # already in the sample
    ],
)
def test_bad_swaps_are_errors(swap):
    with pytest.raises(InvalidSwap):
        apply_swaps(("A", "B", "C"), {"A", "B", "C", "D"}, [swap])


# --------------------------------------------------------------------------- #
# validate.samples.SAMPLE_V1
# --------------------------------------------------------------------------- #


def test_fixture_lists_are_the_lists_the_draw_used():
    for d in SAMPLE_V1.draws:
        assert hashlib.sha256(LISTS[d.index_code].read_bytes()).hexdigest() == d.list_content_hash


def test_v1_draw_reproduces_from_the_lists():
    for d in SAMPLE_V1.draws:
        members = _constituents(d.index_code)
        assert seeded_draw(members, seed=SAMPLE_V1.seed, n=d.n) == d.drawn_isins
        assert all(members[isin] == symbol for isin, symbol in d.drawn)


def test_v1_final_sample_is_20_distinct_companies_with_every_required_type():
    populations = {code: frozenset(_constituents(code)) for code in LISTS}
    final = SAMPLE_V1.final(populations)
    assert [len(v) for v in final.values()] == [10, 10]
    everyone = SAMPLE_V1.isins()
    assert len(set(everyone)) == 20 and set(everyone) == {i for v in final.values() for i in v}
    for kind, covering in SAMPLE_V1.coverage:
        assert covering and set(covering) <= set(everyone), kind
    assert set(everyone) <= set(SAMPLE_V1.symbols)


# A definition is never edited: a change is a new version. Add a digest when
# adding a sample; never change a recorded one.
FROZEN_DIGESTS = {"validation-sample/1": "dee749840d78346f712243b78abc466a29034f9f051cc3aac1a457030086f347"}


def _digest(sample) -> str:
    return hashlib.sha256(repr(sample).encode()).hexdigest()


def test_recorded_definitions_are_frozen():
    assert set(SAMPLES) == set(FROZEN_DIGESTS)
    for version, sample in SAMPLES.items():
        assert _digest(sample) == FROZEN_DIGESTS[version], f"{version} changed; add a new version instead"

