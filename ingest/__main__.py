"""python -m ingest list | run NAME [NAME ...] | canary [NAME ...]

Exit codes: 0 when every adapter succeeded, was disabled or was skipped;
1 when any failed; 2 when startup was refused or a name is unknown.
`canary` with no names checks every adapter; schedule it daily.
"""

import argparse
import sys
import traceback
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.blob import s3_blob_store
from core.config import get_settings
from core.timezones import IST
from ingest.base import AdapterContext
from ingest.registry import StartupRefused, check_startup, discover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("run").add_argument("names", nargs="+")
    sub.add_parser("canary").add_argument("names", nargs="*")
    args = parser.parse_args(argv)

    settings = get_settings()
    adapters = discover()
    try:
        check_startup(settings, adapters)
    except StartupRefused as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    if args.command == "list":
        for name, cls in sorted(adapters.items()):
            state = cls.disabled_reason(settings) or "enabled"
            print(f"{name}\t{cls.source_class}\t{','.join(cls.target_stores)}\t{state}")
        return 0

    names = args.names or sorted(adapters)
    if unknown := [n for n in names if n not in adapters]:
        print(f"unknown adapters: {unknown}", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url)
    blob = s3_blob_store(settings)
    exit_code = 0
    for name in names:
        adapter = adapters[name]()
        with Session(engine) as session:
            ctx = AdapterContext(session, blob, settings, now=lambda: datetime.now(IST))
            try:
                result = adapter.run(ctx) if args.command == "run" else adapter.canary(ctx)
                session.commit()
            except Exception:
                session.rollback()
                traceback.print_exc()
                print(f"{name}\tfailed")
                exit_code = 1
                continue
        print(f"{name}\t{result.status}\t{result.detail}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
