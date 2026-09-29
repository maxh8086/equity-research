#!/usr/bin/env python
"""Generate a guidance report for a company.

Usage: python report_cli.py --symbol RELIANCE --as-of 2026-03-31 [--quarter-start ...]

Prints the guidance report (rendered as markdown) to stdout, showing what management
promised, what landed, what went silent, and the hedge-weighted delivery rate.

Exit codes: 0 on success; 1 on symbol not found; 2 on bad arguments or database error.
"""

import argparse
import sys
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.compute.guidance_report import render_markdown
from core.config import get_settings
from core.db.pit import symbol_to_isin_as_of
from core.timezones import IST

# Lazy import to allow testing without the function existing in pit.py
def _get_guidance_report_as_of():
    from core.db.pit import guidance_report_as_of
    return guidance_report_as_of


def _date(text: str) -> date:
    """Parse a date string YYYY-MM-DD."""
    return datetime.fromisoformat(text).date()


def _datetime(text: str) -> datetime:
    """Parse a date string YYYY-MM-DD to a timezone-aware IST datetime at start of day."""
    value = _date(text)
    # Create a datetime at the start of the day in IST timezone
    return datetime.combine(value, datetime.min.time(), tzinfo=IST)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python report_cli.py",
        description="Generate a guidance report for a company.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 on success; 1 on symbol not found; 2 on bad arguments or database error.",
    )
    parser.add_argument("--symbol", required=True, help="Stock symbol (e.g., RELIANCE)")
    parser.add_argument("--as-of", required=True, type=_datetime, dest="as_of", help="Report date (YYYY-MM-DD)")
    parser.add_argument("--quarter-start", type=_date, help="Quarter start date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    symbol = args.symbol
    as_of = args.as_of
    quarter_start = args.quarter_start

    # Default quarter_start to the start of the quarter that contains as_of
    if quarter_start is None:
        # Find the quarter start (Jan 1, Apr 1, Jul 1, Oct 1)
        month = as_of.month
        if month < 4:
            quarter_start = date(as_of.year, 1, 1)
        elif month < 7:
            quarter_start = date(as_of.year, 4, 1)
        elif month < 10:
            quarter_start = date(as_of.year, 7, 1)
        else:
            quarter_start = date(as_of.year, 10, 1)

    try:
        settings = get_settings()
        engine = create_engine(settings.database_url)
    except Exception as e:
        print(f"Database configuration error: {e}", file=sys.stderr)
        return 2

    try:
        guidance_report_as_of = _get_guidance_report_as_of()
        with Session(engine) as session:
            # Resolve symbol to ISIN using the latest trade before as_of
            isin = symbol_to_isin_as_of(session, symbol=symbol, on=as_of.date(), as_of=as_of)
            if isin is None:
                print(f"Symbol {symbol!r} not found as of {as_of.date().isoformat()}", file=sys.stderr)
                return 1

            # Get the guidance report
            report = guidance_report_as_of(session, isin=isin, symbol=symbol, as_of=as_of, quarter_start=quarter_start)

            # Render and print to stdout
            print(render_markdown(report))
            return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
