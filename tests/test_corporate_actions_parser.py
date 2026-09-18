"""Corporate-action parsers: NSE export PURPOSE text and the curated file. No database."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.db.models import CorporateActionQuarantineReason as Reason
from core.db.models import CorporateActionStatus as Status
from core.db.models import CorporateActionType as Kind
from core.db.models import RatioBasis
from core.timezones import IST
from ingest.corporate_actions.parser import (
    CURATED_COLUMNS,
    NSE_COLUMNS,
    FileRejected,
    PurposeUnparsed,
    nse_row_as_of,
    parse_curated_csv,
    parse_nse_csv,
    parse_purpose,
    terms_problem,
    with_isin,
)

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"
DOWNLOADED = datetime(2024, 10, 1, 20, 0, tzinfo=IST)
URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


# --------------------------------------------------------------------------- #
# PURPOSE text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("purpose", "face_value", "expected"),
    [
        ("Bonus 1:1", None, [{"action_type": Kind.BONUS, "shares_new": 1, "shares_held": 1}]),
        ("Bonus 3:2", None, [{"action_type": Kind.BONUS, "shares_new": 3, "shares_held": 2}]),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
            D(10),
            [{"action_type": Kind.SPLIT, "face_value_from": D(10), "face_value_to": D(2)}],
        ),
        (
            "Consolidation Of Shares From Re 1/- Per Share To Rs 10/- Per Share",
            D(1),
            [{"action_type": Kind.CONSOLIDATION, "face_value_from": D(1), "face_value_to": D(10)}],
        ),
        (
            "Rights 1:4 @ Premium Rs 50/-",
            D(10),
            [{"action_type": Kind.RIGHTS, "shares_new": 1, "shares_held": 4, "issue_price": D(60)}],
        ),
        (
            "Rights 2:7 @ Rs 1257/-",
            D(10),
            [{"action_type": Kind.RIGHTS, "shares_new": 2, "shares_held": 7, "issue_price": D(1257)}],
        ),
        ("Interim Dividend - Rs 21 Per Share", D(5), [{"action_type": Kind.DIVIDEND, "dividend_per_share": D(21)}]),
        (
            "Final Dividend - Rs 8 Per Share/Special Dividend - Rs 3.50 Per Share",
            D(5),
            [
                {"action_type": Kind.DIVIDEND, "dividend_per_share": D(8)},
                {"action_type": Kind.DIVIDEND, "dividend_per_share": D("3.50")},
            ],
        ),
        ("Annual General Meeting", D(5), []),
        ("Interest Payment", D(1000), []),
    ],
)
def test_purpose_patterns(purpose, face_value, expected):
    assert parse_purpose(purpose, face_value) == expected


@pytest.mark.parametrize(
    ("purpose", "reason"),
    [
        ("Demerger", Reason.RATIO_NOT_IN_EXCHANGE_FIELD),
        ("Scheme Of Arrangement - De-merger", Reason.RATIO_NOT_IN_EXCHANGE_FIELD),
        ("Bonus Issue", Reason.UNPARSED_PURPOSE),
        ("Face Value Split", Reason.UNPARSED_PURPOSE),
        ("Face Value Split From Rs 2/- To Rs 10/-", Reason.UNPARSED_PURPOSE),  # that is a consolidation
        ("Rights Issue", Reason.UNPARSED_PURPOSE),
        ("Dividend", Reason.UNPARSED_PURPOSE),
        ("Final Dividend - Rs 8 Per Share/Special Dividend", Reason.UNPARSED_PURPOSE),  # one amount of two
    ],
)
def test_an_action_named_without_readable_terms_is_never_guessed(purpose, reason):
    with pytest.raises(PurposeUnparsed) as exc:
        parse_purpose(purpose, D(10))
    assert exc.value.reason is reason


def test_rights_premium_needs_the_face_value():
    with pytest.raises(PurposeUnparsed):
        parse_purpose("Rights 1:4 @ Premium Rs 50/-", None)


# --------------------------------------------------------------------------- #
# Row as_of
# --------------------------------------------------------------------------- #


def test_row_as_of_is_the_earlier_of_download_and_ex_date_start():
    assert nse_row_as_of(DOWNLOADED, date(2024, 10, 28)) == DOWNLOADED
    assert nse_row_as_of(DOWNLOADED, date(2024, 8, 19)) == datetime(2024, 8, 19, tzinfo=IST)
    assert nse_row_as_of(DOWNLOADED, None) == DOWNLOADED


def test_row_as_of_refuses_a_naive_time():
    with pytest.raises(ValueError):
        nse_row_as_of(datetime(2024, 10, 1), date(2024, 10, 28))


# --------------------------------------------------------------------------- #
# NSE export
# --------------------------------------------------------------------------- #


def _nse(*rows: str) -> bytes:
    return ("\n".join([",".join(NSE_COLUMNS), *rows]) + "\n").encode()


def test_fixture_file():
    parsed = parse_nse_csv((FIXTURES / "nse_corporate_actions_sample.csv").read_bytes(),
                           file_as_of=DOWNLOADED, source_url=URL)  # fmt: skip
    by_row = {}
    for a in parsed.actions:
        by_row.setdefault(a.row_number, []).append(a)
    assert sorted(by_row) == [1, 2, 3, 4, 5]
    bonus = by_row[1][0]
    assert (bonus.action_type, bonus.shares_new, bonus.shares_held, bonus.symbol) == (Kind.BONUS, 1, 1, "RELIANCE")
    assert bonus.status is Status.DATES_SET and bonus.ratio_basis is RatioBasis.EXCHANGE_FIELD
    assert bonus.as_of == DOWNLOADED  # ex-date after the download
    assert bonus.key == "bonus:2024-10-28:1" and bonus.isin is None
    agm_dividend = by_row[3][0]
    assert agm_dividend.dividend_per_share == D(10)
    assert agm_dividend.as_of == datetime(2024, 8, 19, tzinfo=IST)  # ex-date before the download
    assert by_row[5][0].issue_price == D(55)  # premium 50 over face value 5

    issues = {i.row_number: i.reason for i in parsed.issues}
    assert issues == {7: Reason.RATIO_NOT_IN_EXCHANGE_FIELD, 8: Reason.UNPARSED_PURPOSE, 10: Reason.MALFORMED_ROW}
    assert parsed.out_of_scope == 2  # the plain AGM and the N1 series


def test_a_missing_column_rejects_the_whole_file():
    header = ",".join(c for c in NSE_COLUMNS if c != "EX-DATE")
    with pytest.raises(FileRejected) as exc:
        parse_nse_csv(f"{header}\n".encode(), file_as_of=DOWNLOADED, source_url=URL)
    assert exc.value.reason is Reason.SHAPE_CHANGED


def test_a_short_row_is_quarantined_and_the_rest_kept():
    parsed = parse_nse_csv(
        _nse("RELIANCE,Reliance,EQ,Bonus 1:1", "INFY,Infosys,EQ,Bonus 1:1,5,28-Oct-2024,28-Oct-2024,-,-"),
        file_as_of=DOWNLOADED, source_url=URL,
    )  # fmt: skip
    assert [i.reason for i in parsed.issues] == [Reason.MALFORMED_ROW]
    assert [a.symbol for a in parsed.actions] == ["INFY"]


def test_two_rows_stating_the_same_action_both_go_to_review():
    row = "INFY,Infosys,EQ,Bonus 1:1,5,28-Oct-2024,28-Oct-2024,-,-"
    parsed = parse_nse_csv(_nse(row, row), file_as_of=DOWNLOADED, source_url=URL)
    assert parsed.actions == ()
    assert [(i.row_number, i.reason) for i in parsed.issues] == [
        (1, Reason.DUPLICATE_ACTION),
        (2, Reason.DUPLICATE_ACTION),
    ]


def test_an_unset_ex_date_is_an_announcement():
    parsed = parse_nse_csv(_nse("INFY,Infosys,EQ,Bonus 1:1,5,-,-,-,-"), file_as_of=DOWNLOADED, source_url=URL)
    (action,) = parsed.actions
    assert action.status is Status.ANNOUNCED and action.ex_date is None and action.as_of == DOWNLOADED


def test_with_isin_completes_the_key():
    parsed = parse_nse_csv(_nse("INFY,Infosys,EQ,Bonus 1:1,5,28-Oct-2024,-,-,-"), file_as_of=DOWNLOADED,
                           source_url=URL)  # fmt: skip
    action = with_isin(parsed.actions[0], "INE009A01021")
    assert action.key == "nse:INE009A01021:bonus:2024-10-28:1"
    assert terms_problem(action) is None
    assert terms_problem(with_isin(parsed.actions[0], "INE009A01020")) is not None  # bad check digit


# --------------------------------------------------------------------------- #
# Curated file
# --------------------------------------------------------------------------- #

SAVED = datetime(2024, 10, 2, 9, 0, tzinfo=IST)


def test_curated_fixture():
    parsed = parse_curated_csv((FIXTURES / "curated_sample.csv").read_bytes(), file_as_of=SAVED)
    demerger, isin_change = parsed.actions
    assert demerger.action_type is Kind.DEMERGER and demerger.retained_fraction == D("0.85")
    assert demerger.ratio_basis is RatioBasis.HUMAN_VERIFIED and demerger.verified_by == "family-reviewer"
    assert demerger.as_of == datetime(2024, 8, 20, 18, 30, tzinfo=IST)  # announced_at, not the save time
    assert demerger.source_url.startswith("https://www.bseindia.com/")
    assert isin_change.new_isin == "INE009A01039"
    assert [(i.row_number, i.reason) for i in parsed.issues] == [(3, Reason.MALFORMED_ROW)]


def _curated(**overrides) -> bytes:
    row = dict.fromkeys(CURATED_COLUMNS, "") | {
        "action_key": "curated:bonus:x", "isin": "INE009A01021", "action_type": "bonus", "status": "dates_set",
        "announced_at": "2024-09-01T10:00:00+05:30", "ex_date": "2024-09-30", "shares_new": "1",
        "shares_held": "2", "evidence_url": "https://example.org/e.pdf", "verified_by": "reviewer",
    } | overrides  # fmt: skip
    return (",".join(CURATED_COLUMNS) + "\n" + ",".join(row[c] for c in CURATED_COLUMNS) + "\n").encode()


def test_curated_row_is_accepted():
    (action,) = parse_curated_csv(_curated(), file_as_of=SAVED).actions
    assert (action.shares_new, action.shares_held, action.key) == (1, 2, "curated:bonus:x")


@pytest.mark.parametrize(
    "overrides",
    [
        {"verified_by": ""},  # an unnamed verification is none
        {"announced_at": "2024-09-01T10:00:00"},  # naive
        {"announced_at": "2024-10-05T10:00:00+05:30"},  # after the file was saved
        {"announced_at": "2024-10-01T10:00:00+05:30"},  # after the ex-date
        {"shares_held": ""},
        {"action_type": "merger"},
        {"evidence_url": "not a url"},
        {"isin": "INE009A01020"},  # bad check digit
    ],
)
def test_curated_rows_that_are_refused(overrides):
    parsed = parse_curated_csv(_curated(**overrides), file_as_of=SAVED)
    assert parsed.actions == () and len(parsed.issues) == 1


def test_curated_withdrawal_needs_no_terms():
    parsed = parse_curated_csv(_curated(status="withdrawn", shares_new="", shares_held=""), file_as_of=SAVED)
    assert parsed.actions[0].status is Status.WITHDRAWN


def test_curated_header_must_match_exactly():
    with pytest.raises(FileRejected):
        parse_curated_csv(b"action_key,isin\n", file_as_of=SAVED)
