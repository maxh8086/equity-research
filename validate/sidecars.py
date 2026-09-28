"""Sidecars for Screener exports saved by hand (python -m validate screener-sidecars).

Every value is derived by code, never guessed:
- the company: the file name equals a sample symbol, or the workbook's company
  name equals the company name in the constituent list the sample was drawn
  from, after normalising case, punctuation and the Ltd/Limited suffix. No
  match, or more than one, leaves the file without a sidecar;
- `published_at`: the file's modification time, which is when the download
  finished (the export time);
- `consolidation`: the view that was exported, which the workbook does not
  record, so the person running the command states it (consolidated unless
  --standalone).

An existing sidecar is never overwritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.timezones import IST
from ingest.base import META_SUFFIX
from ingest.screener_export.adapters import XLSX
from ingest.screener_export.parser import ExportRejected, parse_export

_SUFFIX = re.compile(r"\s+(ltd|limited)$")


def normalise_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and "))
    text = re.sub(r"\s+", " ", text).strip()
    return _SUFFIX.sub("", text)


@dataclass(frozen=True)
class Company:
    isin: str
    symbol: str
    listed_name: str


def match_company(stem: str, company_name: str, companies: list[Company]) -> Company | str:
    """The one company this export belongs to, or the reason there is none."""
    by_symbol = [c for c in companies if c.symbol.upper() == stem.strip().upper()]
    by_name = [c for c in companies if normalise_name(c.listed_name) == normalise_name(company_name)]
    found = {c.isin: c for c in by_symbol + by_name}
    if len(found) == 1:
        return next(iter(found.values()))
    if not found:
        return f"workbook company {company_name!r} matches no sample company; rename the file to its NSE symbol"
    return f"file name and workbook company point to different companies: {sorted(c.symbol for c in found.values())}"


def sidecar(company: Company, published_at: datetime, *, consolidated: bool) -> dict[str, str]:
    view = "consolidated" if consolidated else "standalone"
    url = f"https://www.screener.in/company/{company.symbol}/" + ("consolidated/" if consolidated else "")
    return {"source_url": url, "published_at": published_at.isoformat(), "media_type": XLSX,
            "isin": company.isin, "consolidation": view}  # fmt: skip


def write_sidecars(folder: Path, companies: list[Company], *, consolidated: bool) -> tuple[list[str], int]:
    """Write a sidecar for each .xlsx in `folder` that lacks one. Returns (lines to print, failures)."""
    lines, failures = [], 0
    for path in sorted(folder.glob("*.xlsx")):
        meta_path = path.with_name(path.name + META_SUFFIX)
        if meta_path.exists():
            lines.append(f"  kept     {path.name}: sidecar already exists")
            continue
        try:
            parsed = parse_export(path.read_bytes())
        except ExportRejected as exc:
            lines.append(f"  REJECTED {path.name}: {exc}")
            failures += 1
            continue
        found = match_company(path.stem, parsed.company_name, companies)
        if isinstance(found, str):
            lines.append(f"  REJECTED {path.name}: {found}")
            failures += 1
            continue
        published_at = datetime.fromtimestamp(path.stat().st_mtime, tz=IST).replace(microsecond=0)
        with meta_path.open("x", encoding="utf-8") as out:
            json.dump(sidecar(found, published_at, consolidated=consolidated), out, indent=2)
        lines.append(f"  wrote    {path.name}: {found.symbol} {found.isin}, exported {published_at.isoformat()}")
    return lines, failures
