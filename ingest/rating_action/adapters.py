"""Rating action drop-folder adapter (CLAUDE.md "Data sources": manual_drop fallback).

Rating actions from CRISIL, ICRA, CARE, India Ratings, Brickwork and Acuite
are ingested from CSV files downloaded by hand into the drop folder.

CSV columns: date, isin, agency, instrument_type, raw_rating, evidence_url

All computations (scale mapping, direction, severity) are done by code in
core.compute.ratings — no model is involved (R1). The latest prior rating for
the same (isin, agency) is looked up from the database to compute direction.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from pydantic import field_validator

from core.compute.ratings import compute_rating_direction, compute_severity, map_rating
from core.db.models import RatingAction, RatingAgency
from core.db.pit import latest_rating_as_of
from core.sources import SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.schema import StrictModel

RULE_VERSION = "rating_action_drop_v1"


class RatingActionRow(StrictModel):
    date: date
    isin: str
    agency: RatingAgency
    instrument_type: str
    raw_rating: str
    evidence_url: str

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, v: str) -> str:
        import re
        if not re.match(r"^IN[A-Z0-9]{9}[0-9]$", v):
            raise ValueError(f"invalid ISIN: {v!r}")
        return v

    @field_validator("evidence_url")
    @classmethod
    def _evidence_url_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence_url must not be empty")
        return v


def _parse_csv(data: bytes) -> list[RatingActionRow]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    rows = []
    for r in reader:
        rows.append(
            RatingActionRow.model_validate(
                {
                    "date": r["date"].strip(),
                    "isin": r["isin"].strip(),
                    "agency": r["agency"].strip().lower(),
                    "instrument_type": r["instrument_type"].strip(),
                    "raw_rating": r["raw_rating"].strip(),
                    "evidence_url": r["evidence_url"].strip(),
                }
            )
        )
    return rows


def _day_end_ist(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST)


class RatingActionDrop(DropFolderAdapter):
    """Rating action CSVs downloaded by hand into <EQUITY_DROP_FOLDER>/rating_action_drop/.

    Each CSV needs a sidecar .meta.json. `published_at` is the date the agency
    published the rating list (use 23:59:59+05:30 on that date for a date-only
    source). Direction and severity are computed by code from the hardcoded
    scale tables in core.compute.ratings (R1).
    """

    name = "rating_action_drop"
    source_class = SourceClass.MANUAL_DROP
    target_stores = ("rating_action",)

    def ingest(self, ctx: AdapterContext) -> RunResult:
        rows_written = 0
        raw_files = 0
        problems: list[str] = []

        for doc in self.drop_files(ctx):
            raw_files += 1
            try:
                written = _load_rating_actions(self, ctx, doc)
                rows_written += written
            except Exception as exc:
                problems.append(f"{doc.source_url}: {exc}")

        detail = f"{rows_written} rows written from {raw_files} files"
        if problems:
            detail += "; " + "; ".join(problems)
        status = RunStatus.FAILED if problems else RunStatus.SUCCEEDED
        return RunResult(
            self.name,
            status,
            detail,
            raw_files=raw_files,
            rows_written=rows_written,
        )

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            _parse_csv(path.read_bytes())


def _load_rating_actions(
    adapter: RatingActionDrop,
    ctx: AdapterContext,
    doc: RawDocument,
) -> int:
    prov = dict(
        as_of=doc.as_of,
        content_hash=doc.content_hash,
        source_url=doc.source_url,
        extracted_by=adapter.extracted_by(),
        model_version=None,
    )
    parsed_rows = _parse_csv(doc.data)
    written = 0
    for row in parsed_rows:
        common_scale, outlook = map_rating(row.agency, row.raw_rating)

        # Look up the latest prior rating for direction computation.
        as_of_t = _day_end_ist(row.date)
        # Prior means strictly before this action_date; use start of day as boundary.
        prior_as_of = datetime(row.date.year, row.date.month, row.date.day, 0, 0, 0, tzinfo=IST)
        prior = latest_rating_as_of(ctx.session, isin=row.isin, agency=row.agency, t=prior_as_of)
        prior_scale = prior.common_scale if prior is not None else None

        is_upgrade, is_downgrade = compute_rating_direction(prior_scale, common_scale)
        is_withdrawn = common_scale.value == "withdrawn"
        severity = compute_severity(
            common_scale,
            is_withdrawn=is_withdrawn,
            is_downgrade=bool(is_downgrade),
            is_upgrade=bool(is_upgrade),
        )

        ctx.session.add(
            RatingAction(
                isin=row.isin,
                agency=row.agency,
                instrument_type=row.instrument_type,
                raw_rating=row.raw_rating,
                common_scale=common_scale,
                outlook=outlook,
                action_date=row.date,
                severity=severity,
                is_upgrade=is_upgrade,
                is_downgrade=is_downgrade,
                is_withdrawn=is_withdrawn,
                evidence_url=row.evidence_url,
                rule_version=RULE_VERSION,
                **prov,
            )
        )
        written += 1

    ctx.session.flush()
    return written
