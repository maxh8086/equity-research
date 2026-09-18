"""Parsers for corporate-action files, shared by every adapter that reads them.

Two formats:

- NSE's corporate-actions CSV export (nseindia.com, Corporate Filings >
  Corporate Actions > Download .csv), downloaded by hand. Columns are read by
  name. It carries no ISIN (the adapter resolves the symbol through the dated
  bhavcopy map) and states each action as free text in PURPOSE, e.g.
  "Bonus 1:1" or "Face Value Split (Sub-Division) - From Rs 10/- Per Share To
  Rs 2/- Per Share". Code parses that text with fixed patterns (R1); text that
  names an action in scope but does not match a pattern is quarantined, never
  guessed. Built from the export's layout as remembered, not a recorded file:
  see tests/fixtures/corporate_actions/SOURCES.md.

- The curated file: one row per action, entered by a named person from the
  evidence (a filing PDF, a scheme of arrangement). For what the exchange
  export cannot state: demerger cost apportionment, ISIN changes, and any
  ratio read from a PDF. Its rows are `human_verified`.

`as_of` of an NSE row: the export has no announcement time, so it is the
earlier of the file's download time and 00:00 IST on the ex-date. An ex-date
is always announced before it arrives, so that is never earlier than the
public knew (docs/temporal-model.md, date-only sources).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from pydantic import AwareDatetime, ConfigDict, Field, ValidationError, field_validator

from core.compute.isin import is_valid_isin
from core.db.models import (
    CorporateActionQuarantineReason as Reason,
)
from core.db.models import (
    CorporateActionStatus as Status,
)
from core.db.models import (
    CorporateActionType as Kind,
)
from core.db.models import (
    RatioBasis,
)
from core.timezones import IST, require_aware
from ingest.schema import StrictModel

RULE_VERSION = "corporate_actions/1"

PRELIMINARY = frozenset({Status.ANNOUNCED, Status.APPROVED, Status.WITHDRAWN})

# --------------------------------------------------------------------------- #
# One parsed action, whichever file it came from
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParsedAction:
    row_number: int  # 1-based data row in the file
    raw_row: str
    action_type: Kind
    status: Status
    as_of: datetime
    source_url: str
    ratio_basis: RatioBasis
    key: str  # full action_key for curated rows; the part after "nse:<isin>:" for NSE rows
    isin: str | None = None  # None until an NSE row's symbol is resolved
    symbol: str | None = None
    ex_date: date | None = None
    record_date: date | None = None
    face_value_from: Decimal | None = None
    face_value_to: Decimal | None = None
    shares_new: int | None = None
    shares_held: int | None = None
    issue_price: Decimal | None = None
    dividend_per_share: Decimal | None = None
    retained_fraction: Decimal | None = None
    new_isin: str | None = None
    verified_by: str | None = None
    purpose: str | None = None


@dataclass(frozen=True)
class RowIssue:
    row_number: int
    raw_row: str
    reason: Reason
    detail: str
    isin: str | None = None


@dataclass(frozen=True)
class ParsedFile:
    actions: tuple[ParsedAction, ...]
    issues: tuple[RowIssue, ...]
    out_of_scope: int = 0  # rows about actions this store does not hold (AGMs, interest payments, ...)


class FileRejected(Exception):
    """The whole file is unusable. Its raw bytes are kept; nothing else is stored."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def terms_problem(a: ParsedAction) -> str | None:
    """What the database would refuse about this action, found before it gets there."""
    if a.isin is not None and not is_valid_isin(a.isin):
        return f"invalid ISIN {a.isin}"
    if a.new_isin is not None and (not is_valid_isin(a.new_isin) or a.new_isin == a.isin):
        return f"invalid new ISIN {a.new_isin}"
    if a.ex_date is not None and a.status is not Status.WITHDRAWN and a.as_of.astimezone(IST).date() > a.ex_date:
        return f"known at {a.as_of.isoformat()}, after its ex-date {a.ex_date}"
    if a.status in PRELIMINARY:
        return None
    if a.ex_date is None:
        return f"status {a.status} needs an ex-date"
    fv_from, fv_to = a.face_value_from, a.face_value_to
    ok = {
        Kind.SPLIT: fv_from is not None and fv_to is not None and 0 < fv_to < fv_from,
        Kind.CONSOLIDATION: fv_from is not None and fv_to is not None and 0 < fv_from < fv_to,
        Kind.BONUS: (a.shares_new or 0) > 0 and (a.shares_held or 0) > 0,
        Kind.RIGHTS: (a.shares_new or 0) > 0 and (a.shares_held or 0) > 0
        and a.issue_price is not None and a.issue_price >= 0,
        Kind.DIVIDEND: a.dividend_per_share is not None and a.dividend_per_share >= 0,
        Kind.DEMERGER: a.retained_fraction is not None and 0 < a.retained_fraction < 1,
        Kind.ISIN_CHANGE: a.new_isin is not None,
    }[a.action_type]
    return None if ok else f"{a.action_type} terms missing or impossible"


# --------------------------------------------------------------------------- #
# NSE corporate-actions CSV export
# --------------------------------------------------------------------------- #

NSE_COLUMNS = (
    "SYMBOL", "COMPANY NAME", "SERIES", "PURPOSE", "FACE VALUE",
    "EX-DATE", "RECORD DATE", "BOOK CLOSURE START DATE", "BOOK CLOSURE END DATE",
)  # fmt: skip

# Mirrors ingest/nse_bhavcopy/parser.py: series confirmed to be ordinary equity.
EQUITY_SERIES = frozenset({"EQ", "BE"})

_DATE_FORMAT = "%d-%b-%Y"  # 20-Aug-2024
_BLANK = frozenset({"", "-", "NA", "N.A."})
_RS = r"(?:Rs\.?|Re\.?|INR|₹)\s*"
_NUM = r"(\d+(?:\.\d+)?)"

# Research decision: which PURPOSE words name an action in scope, and the
# patterns that read its terms. A keyword without a matching pattern is
# quarantined (unparsed_purpose), so a new NSE wording is seen, not skipped.
_KEYWORDS: dict[Kind, re.Pattern[str]] = {
    Kind.SPLIT: re.compile(r"\bsplit\b|sub-?\s*division", re.I),
    Kind.CONSOLIDATION: re.compile(r"\bconsolidation\b", re.I),
    Kind.BONUS: re.compile(r"\bbonus\b", re.I),
    Kind.RIGHTS: re.compile(r"\brights\b", re.I),
    Kind.DIVIDEND: re.compile(r"\bdividend\b", re.I),
    Kind.DEMERGER: re.compile(r"\bde-?merger\b", re.I),
}
_FROM_TO = re.compile(rf"from\s*{_RS}{_NUM}.*?\bto\s*{_RS}{_NUM}", re.I)
_BONUS = re.compile(r"\bbonus\b\s*-?\s*(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS = re.compile(rf"\brights\b\s*-?\s*(\d+)\s*:\s*(\d+)\s*@\s*(premium\s*(?:of\s*)?)?{_RS}{_NUM}", re.I)
_DIVIDEND = re.compile(rf"\bdividend\b\s*-?\s*{_RS}{_NUM}\s*(?:/-)?\s*per\s*share", re.I)


def _nse_date(value: str, column: str) -> date | None:
    value = value.strip()
    if value.upper() in _BLANK:
        return None
    try:
        return datetime.strptime(value, _DATE_FORMAT).date()
    except ValueError:
        raise ValueError(f"{column}: {value!r} is not DD-Mon-YYYY") from None


def _decimal(value: str, column: str) -> Decimal | None:
    value = value.strip()
    if value.upper() in _BLANK:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{column}: {value!r} is not a number") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{column}: {value!r} is not a non-negative number")
    return number


class PurposeUnparsed(Exception):
    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_purpose(purpose: str, face_value: Decimal | None) -> list[dict]:
    """The actions in scope named by one PURPOSE text, as term dicts. [] if none are.

    Raises PurposeUnparsed when an in-scope action is named but its terms
    cannot be read. Every part of the text must parse, or none is used.
    """
    terms: list[dict] = []
    named = {kind for kind, pattern in _KEYWORDS.items() if pattern.search(purpose)}
    if Kind.DEMERGER in named:
        raise PurposeUnparsed(
            Reason.RATIO_NOT_IN_EXCHANGE_FIELD,
            "a demerger's cost apportionment is not in the exchange field: enter it in the curated file",
        )
    for kind in (Kind.SPLIT, Kind.CONSOLIDATION):
        if kind not in named:
            continue
        match = _FROM_TO.search(purpose)
        if not match:
            raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, f"{kind} without 'From Rs X To Rs Y'")
        fv_from, fv_to = Decimal(match[1]), Decimal(match[2])
        if (kind is Kind.SPLIT) != (fv_to < fv_from) or fv_to == fv_from or not fv_to > 0:
            raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, f"{kind} from {fv_from} to {fv_to} is the wrong way round")
        terms.append({"action_type": kind, "face_value_from": fv_from, "face_value_to": fv_to})
    if Kind.BONUS in named:
        match = _BONUS.search(purpose)
        if not match:
            raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, "bonus without an 'a:b' ratio")
        terms.append({"action_type": Kind.BONUS, "shares_new": int(match[1]), "shares_held": int(match[2])})
    if Kind.RIGHTS in named:
        match = _RIGHTS.search(purpose)
        if not match:
            raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, "rights without 'a:b @ [Premium] Rs X'")
        price = Decimal(match[4])
        if match[3]:  # a premium is over the face value
            if face_value is None:
                raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, "rights premium with no face value")
            price += face_value
        terms.append(
            {"action_type": Kind.RIGHTS, "shares_new": int(match[1]), "shares_held": int(match[2]), "issue_price": price}
        )
    if Kind.DIVIDEND in named:
        amounts = _DIVIDEND.findall(purpose)
        mentions = len(_KEYWORDS[Kind.DIVIDEND].findall(purpose))
        if len(amounts) != mentions:
            raise PurposeUnparsed(Reason.UNPARSED_PURPOSE, f"{mentions} dividends named, {len(amounts)} amounts read")
        terms.extend({"action_type": Kind.DIVIDEND, "dividend_per_share": Decimal(a)} for a in amounts)
    return terms


def nse_row_as_of(file_as_of: datetime, ex_date: date | None) -> datetime:
    """The earlier of when we had the file and the start of the ex-date (IST)."""
    require_aware(file_as_of, "file_as_of")
    if ex_date is None:
        return file_as_of
    return min(file_as_of, datetime.combine(ex_date, time(0, 0), tzinfo=IST))


def _raw(row: dict[str, str]) -> str:
    return json.dumps(row, ensure_ascii=False)


def _read_csv(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejected(Reason.SHAPE_CHANGED, f"not UTF-8: {exc}") from None
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip() for h in next(reader)]
    except (StopIteration, csv.Error) as exc:
        raise FileRejected(Reason.SHAPE_CHANGED, f"no header row: {exc}") from None
    if len(set(header)) != len(header):
        raise FileRejected(Reason.SHAPE_CHANGED, f"repeated columns in {header}")
    rows = []
    try:
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            if len(values) != len(header):
                rows.append({"__malformed__": json.dumps(values, ensure_ascii=False)})
                continue
            rows.append(dict(zip(header, values, strict=True)))
    except csv.Error as exc:
        raise FileRejected(Reason.SHAPE_CHANGED, f"unreadable CSV: {exc}") from None
    return header, rows


def parse_nse_csv(data: bytes, *, file_as_of: datetime, source_url: str) -> ParsedFile:
    """Validate one NSE export. Actions carry `symbol`; the adapter resolves the ISIN."""
    header, rows = _read_csv(data)
    if missing := [c for c in NSE_COLUMNS if c not in header]:
        raise FileRejected(Reason.SHAPE_CHANGED, f"missing columns {missing}")

    actions: list[ParsedAction] = []
    issues: list[RowIssue] = []
    out_of_scope = 0
    for number, row in enumerate(rows, start=1):
        if "__malformed__" in row:
            raw = row["__malformed__"]
            issues.append(RowIssue(number, raw, Reason.MALFORMED_ROW, f"expected {len(header)} fields"))
            continue
        raw = _raw(row)
        if row["SERIES"].strip() not in EQUITY_SERIES:
            out_of_scope += 1
            continue
        symbol, purpose = row["SYMBOL"].strip(), " ".join(row["PURPOSE"].split())
        try:
            if not symbol:
                raise ValueError("SYMBOL is empty")
            face_value = _decimal(row["FACE VALUE"], "FACE VALUE")
            ex_date = _nse_date(row["EX-DATE"], "EX-DATE")
            record_date = _nse_date(row["RECORD DATE"], "RECORD DATE")
        except ValueError as exc:
            issues.append(RowIssue(number, raw, Reason.MALFORMED_ROW, str(exc)))
            continue
        try:
            terms = parse_purpose(purpose, face_value)
        except PurposeUnparsed as exc:
            issues.append(RowIssue(number, raw, exc.reason, f"{exc.detail}: {purpose!r}"))
            continue
        if not terms:
            out_of_scope += 1
            continue
        as_of = nse_row_as_of(file_as_of, ex_date)
        status = Status.ANNOUNCED if ex_date is None else Status.DATES_SET
        seen: dict[Kind, int] = {}
        for t in terms:
            kind = t["action_type"]
            seen[kind] = seen.get(kind, 0) + 1
            actions.append(
                ParsedAction(
                    row_number=number, raw_row=raw, status=status, as_of=as_of, source_url=source_url,
                    ratio_basis=RatioBasis.EXCHANGE_FIELD, symbol=symbol, ex_date=ex_date,
                    record_date=record_date, purpose=purpose,
                    key=f"{kind}:{ex_date.isoformat() if ex_date else 'no-ex-date'}:{seen[kind]}", **t,
                )  # fmt: skip
            )
    return _drop_duplicates(actions, issues, out_of_scope, key=lambda a: (a.symbol, a.key))


def _drop_duplicates(actions, issues, out_of_scope, *, key) -> ParsedFile:
    """Two rows stating the same action: neither can be preferred, so both go to review."""
    rows_by_key: dict[object, set[int]] = {}
    for a in actions:
        rows_by_key.setdefault(key(a), set()).add(a.row_number)
    clashing = {n for rows in rows_by_key.values() if len(rows) > 1 for n in rows}
    kept = [a for a in actions if a.row_number not in clashing]
    flagged: dict[int, ParsedAction] = {}
    for a in actions:
        if a.row_number in clashing:
            flagged.setdefault(a.row_number, a)
    issues = [
        *issues,
        *(
            RowIssue(a.row_number, a.raw_row, Reason.DUPLICATE_ACTION, f"{key(a)} is stated by more than one row")
            for a in flagged.values()
        ),
    ]
    return ParsedFile(tuple(kept), tuple(sorted(issues, key=lambda i: i.row_number)), out_of_scope)


def with_isin(action: ParsedAction, isin: str) -> ParsedAction:
    """An NSE action once its symbol is resolved: the ISIN joins the key."""
    return replace(action, isin=isin, key=f"nse:{isin}:{action.key}")


# --------------------------------------------------------------------------- #
# Curated file: human-entered, human-verified
# --------------------------------------------------------------------------- #

CURATED_COLUMNS = (
    "action_key", "isin", "action_type", "status", "announced_at", "ex_date", "record_date",
    "face_value_from", "face_value_to", "shares_new", "shares_held", "issue_price",
    "dividend_per_share", "retained_fraction", "new_isin", "evidence_url", "verified_by", "note",
)  # fmt: skip

_KEY = r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$"  # reusing an NSE key (nse:<isin>:...) versions that action
_ISIN = r"^IN[A-Z0-9]{9}[0-9]$"


class CuratedRow(StrictModel):
    # CSV cells are text: coerce them (lax), but still refuse unknown fields.
    model_config = ConfigDict(extra="forbid", strict=False, frozen=True)

    action_key: str = Field(pattern=_KEY)
    isin: str = Field(pattern=_ISIN)
    action_type: Kind
    status: Status
    announced_at: AwareDatetime
    ex_date: date | None
    record_date: date | None
    face_value_from: Decimal | None = Field(ge=0)
    face_value_to: Decimal | None = Field(ge=0)
    shares_new: int | None = Field(ge=1)
    shares_held: int | None = Field(ge=1)
    issue_price: Decimal | None = Field(ge=0)
    dividend_per_share: Decimal | None = Field(ge=0)
    retained_fraction: Decimal | None = Field(gt=0, lt=1)
    new_isin: str | None = Field(pattern=_ISIN)
    evidence_url: str = Field(pattern=r"^https?://\S+$")
    verified_by: str = Field(min_length=1)
    note: str | None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and value.strip() == "" else value


def parse_curated_csv(data: bytes, *, file_as_of: datetime) -> ParsedFile:
    """Validate one curated file. `file_as_of` is when the file was saved: no row may be announced later."""
    require_aware(file_as_of, "file_as_of")
    header, rows = _read_csv(data)
    if header != list(CURATED_COLUMNS):
        raise FileRejected(Reason.SHAPE_CHANGED, f"columns must be exactly {list(CURATED_COLUMNS)}")

    actions: list[ParsedAction] = []
    issues: list[RowIssue] = []
    for number, row in enumerate(rows, start=1):
        if "__malformed__" in row:
            issues.append(RowIssue(number, row["__malformed__"], Reason.MALFORMED_ROW, f"expected {len(header)} fields"))
            continue
        raw = _raw(row)
        try:
            parsed = CuratedRow.model_validate({k: row[k].strip() for k in CURATED_COLUMNS})
        except ValidationError as exc:
            error = exc.errors()[0]
            issues.append(
                RowIssue(number, raw, Reason.MALFORMED_ROW, f"{'.'.join(map(str, error['loc']))}: {error['msg']}")
            )
            continue
        if parsed.announced_at > file_as_of:
            issues.append(RowIssue(number, raw, Reason.MALFORMED_ROW, "announced after the file was saved"))
            continue
        action = ParsedAction(
            row_number=number, raw_row=raw, action_type=parsed.action_type, status=parsed.status,
            as_of=parsed.announced_at, source_url=parsed.evidence_url, ratio_basis=RatioBasis.HUMAN_VERIFIED,
            key=parsed.action_key, isin=parsed.isin, ex_date=parsed.ex_date, record_date=parsed.record_date,
            face_value_from=parsed.face_value_from, face_value_to=parsed.face_value_to,
            shares_new=parsed.shares_new, shares_held=parsed.shares_held, issue_price=parsed.issue_price,
            dividend_per_share=parsed.dividend_per_share, retained_fraction=parsed.retained_fraction,
            new_isin=parsed.new_isin, verified_by=parsed.verified_by, purpose=parsed.note,
        )  # fmt: skip
        if problem := terms_problem(action):
            reason = Reason.INVALID_ISIN if "ISIN" in problem else Reason.MALFORMED_ROW
            issues.append(RowIssue(number, raw, reason, problem, isin=parsed.isin))
            continue
        actions.append(action)
    return _drop_duplicates(actions, issues, 0, key=lambda a: (a.key, a.as_of))


__all__ = [
    "CURATED_COLUMNS",
    "NSE_COLUMNS",
    "RULE_VERSION",
    "FileRejected",
    "ParsedAction",
    "ParsedFile",
    "RowIssue",
    "nse_row_as_of",
    "parse_curated_csv",
    "parse_nse_csv",
    "parse_purpose",
    "terms_problem",
    "with_isin",
]
