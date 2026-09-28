"""python -m validate sample | screener | screener-sidecars

`sample` recomputes the current sample's draw from the constituent list it
records, read from the database, and checks it against the frozen definition.

`screener` compares our consolidated fiscal-year lines and ratios with the
newest Screener export known at `--as-of`, for every company in the sample
(or `--isin`). It prints the non-matching comparisons with their line items,
then the evidence on leases, non-controlling interests and ROCE.

`screener-sidecars` writes the sidecar for each Screener export saved into
<EQUITY_DROP_FOLDER>/screener_export_drop/ without one (validate/sidecars.py).

Exit codes: 0 when everything checked passes; 1 when anything fails (the
Week 2 gate); 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.compute.sample import seeded_draw
from core.config import get_settings
from core.db.pit import index_list_names_as_of, index_lists_as_of
from core.timezones import IST, require_aware
from validate.report import company_report, render_company, render_findings
from validate.samples import CURRENT, SAMPLES, SampleDefinition
from validate.sidecars import Company, write_sidecars

DEFAULT_YEARS = (2022, 2023, 2024, 2025)


def _datetime(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=IST)


def check_sample(session: Session, sample: SampleDefinition, as_of: datetime) -> int:
    """Recompute each index's draw from the list it records, as known at `as_of`.

    The population is that one list: the newest complete list published by
    `membership_on`. Membership computed from lists is uncertain after the
    last list, so it cannot supply the population on its own.
    """
    require_aware(as_of, "as_of")
    failed = False
    populations = {}
    for d in sample.draws:
        code = d.index_code.value
        lists = index_lists_as_of(session, index_code=d.index_code, as_of=as_of)
        recorded = [x for x in lists if x.content_hash == d.list_content_hash]
        if not recorded:
            print(f"{code}: list {d.list_content_hash[:12]} not known at {as_of.isoformat()}; load it first")
            failed = True
            continue
        listed = recorded[-1]
        newer = [x for x in lists if listed.as_of < x.as_of <= sample.membership_on and x.complete]
        if not listed.complete or newer:
            print(f"{code}: list {d.list_content_hash[:12]} is not the newest complete list at membership_on")
            failed = True
        populations[d.index_code] = listed.isins
        drawn = seeded_draw(listed.isins, seed=sample.seed, n=d.n)
        same = drawn == d.drawn_isins
        print(f"{code}: draw from the list published {listed.as_of.isoformat()} "
              f"{'matches' if same else 'DIFFERS FROM'} {sample.version}")  # fmt: skip
        if not same:
            print(f"  recomputed {list(drawn)}\n  recorded   {list(d.drawn_isins)}")
            failed = True
    if not failed:
        for index_code, isins in sample.final(populations).items():
            print(f"{index_code.value}: " + ", ".join(sample.symbols[i] for i in isins))
        for label, covering in sample.coverage:
            print(f"  {label}: " + ", ".join(sample.symbols[i] for i in covering))
    return 1 if failed else 0


def check_screener(session: Session, isins: list[str], symbols: dict[str, str], years: list[int], as_of: datetime,
                   show_all: bool) -> int:  # fmt: skip
    reports = [company_report(session, isin=i, symbol=symbols.get(i, i), years=years, as_of=as_of) for i in isins]
    for rep in reports:
        print("\n".join(render_company(rep, show_all=show_all)))
    print("\n".join(render_findings(reports)))
    passed = sum(rep.passes for rep in reports)
    verdict = "PASS" if passed == len(reports) else "FAIL"
    print(f"\nWeek 2 gate: {verdict} ({passed}/{len(reports)} companies match, as of {as_of.isoformat()})")
    return 0 if verdict == "PASS" else 1


def sample_companies(session: Session, sample: SampleDefinition, as_of: datetime) -> list[Company]:
    """The final sample, named as in the constituent lists it was drawn from."""
    names: dict[str, str] = {}
    for d in sample.draws:
        names |= index_list_names_as_of(session, content_hash=d.list_content_hash, as_of=as_of)
    return [Company(i, sample.symbols[i], names[i]) for i in sample.isins() if i in names]


def check_sidecars(session: Session, sample: SampleDefinition, folder: Path, *, consolidated: bool) -> int:
    if not folder.is_dir():
        print(f"{folder} does not exist")
        return 1
    companies = sample_companies(session, sample, datetime.now(IST))
    if len(companies) != len(sample.isins()):
        print("the sample's constituent lists are not loaded; run the index list loader first")
        return 1
    lines, failures = write_sidecars(folder, companies, consolidated=consolidated)
    print("\n".join([f"{folder}:", *lines] if lines else [f"{folder}: no .xlsx files"]))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m validate")
    parser.add_argument("--sample", default=CURRENT.version, choices=sorted(SAMPLES))
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--as-of", type=_datetime, help="lists published by then (default: the membership date)")
    c = sub.add_parser("screener")
    c.add_argument("--as-of", type=_datetime, help="what was known then (default: now)")
    c.add_argument("--years", type=int, nargs="+", default=list(DEFAULT_YEARS), help="fiscal years (FY ending March)")
    c.add_argument("--isin", nargs="+", help="compare these instead of the sample")
    c.add_argument("--all", action="store_true", help="print matching comparisons too")
    w = sub.add_parser("screener-sidecars")
    w.add_argument("--standalone", action="store_true", help="the files are standalone exports")
    args = parser.parse_args(argv)

    sample = SAMPLES[args.sample]
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        if args.command == "sample":
            return check_sample(session, sample, args.as_of or sample.membership_on)
        if args.command == "screener-sidecars":
            folder = get_settings().drop_folder
            if folder is None:
                print("EQUITY_DROP_FOLDER is not set")
                return 2
            return check_sidecars(session, sample, folder / "screener_export_drop", consolidated=not args.standalone)
        isins = args.isin or list(sample.isins())
        as_of = args.as_of or datetime.now(IST)
        return check_screener(session, isins, sample.symbols, args.years, as_of, args.all)


if __name__ == "__main__":
    sys.exit(main())
