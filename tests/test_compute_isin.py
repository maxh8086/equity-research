import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.isin import is_valid_isin, isin_check_digit

ALNUM = string.ascii_uppercase + string.digits
bodies = st.builds(
    lambda cc, rest: cc + rest,
    st.text(string.ascii_uppercase, min_size=2, max_size=2),
    st.text(ALNUM, min_size=9, max_size=9),
)


@pytest.mark.parametrize(
    "isin",
    [
        "INE848E01016",  # NHPC
        "INE002A01018",  # Reliance Industries
        "INE467B01029",  # TCS
        "INE009A01021",  # Infosys
        "US0378331005",  # Apple
    ],
)
def test_known_isins_are_valid(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize(
    "isin",
    ["INE848E01017", "ine848e01016", "INE848E0101", "INE848E010166", "1NE848E01016", ""],
)
def test_malformed_or_wrong_check_digit_rejected(isin):
    assert not is_valid_isin(isin)


@given(bodies)
def test_exactly_one_check_digit_validates(body):
    valid = [d for d in range(10) if is_valid_isin(f"{body}{d}")]
    assert valid == [isin_check_digit(body)]


def test_bad_body_raises():
    with pytest.raises(ValueError):
        isin_check_digit("in848e0101")
