"""Order-win adapter over already-ingested NSE announcement raw files (web_scrape lineage).

The bytes it reads came from a web scrape, so it carries the `web_scrape`
source class: off in commercial mode, off without its own switch. It never
fetches. Every field of an `order_win` row is a code-parsed value with its
verbatim quote (R1); no model is involved, so `model_version` is always NULL.

Idempotence: an announcement's identity is a hash of its ISIN, time and text
(`announcement_key`). Inserts are `ON CONFLICT DO NOTHING` on
(announcement_key, rule_version), so a rerun, or the same announcement in an
overlapping later listing, writes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from core.blob import BlobIntegrityError
from core.compute import order_wins
from core.compute.hashing import content_hash
from core.db.models import (
    OrderWinFact,
    OrderWinQuarantine,
    OrderWinQuarantineReason,
    RawSourceFile,
)
from core.db.pit import raw_source_files_as_of
from core.sources import SourceClass
from core.timezones import require_aware
from ingest.base import Adapter, AdapterContext, RunResult, RunStatus
from ingest.nse_announcements.adapters import NseAnnouncements
from ingest.nse_announcements.schema import AnnouncementRow, AnnouncementsListingResponse

TARGET_STORES = ("order_win", "order_win_quarantine")

_ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")


@dataclass
class Tally:
    raw_files: int = 0
    rows_parsed: int = 0
    rows_written: int = 0
    quarantined: int = 0
    not_an_order: int = 0
    already_known: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter_name: str) -> RunResult:
        parts = []
        if self.rows_parsed:
            parts.append(f"{self.rows_parsed} announcements read")
        if self.rows_written:
            parts.append(f"{self.rows_written} order wins written")
        if self.quarantined:
            parts.append(f"{self.quarantined} quarantined")
        if self.not_an_order:
            parts.append(f"{self.not_an_order} not an order")
        if self.already_known:
            parts.append(f"{self.already_known} already known")
        if self.problems:
            parts.append("; ".join(self.problems))
        return RunResult(
            adapter_name,
            RunStatus.FAILED if self.problems else RunStatus.SUCCEEDED,
            ", ".join(parts) if parts else "no stored announcement listings",
            raw_files=0,  # this adapter stores no raw files of its own
            rows_written=self.rows_written,
            quarantined=self.quarantined,
        )


def announcement_key(row: AnnouncementRow) -> str:
    """Identity of one announcement across listings: ISIN, time and text."""
    text = "\x1f".join((row.isin, row.announced_on.isoformat(), row.subject, row.description or ""))
    return content_hash(text.encode("utf-8"))


class NseOrderWins(Adapter):
    """Extract order wins from NSE announcement listings that are already stored."""

    name = "nse_order_wins"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        now = require_aware(ctx.now(), "now()")
        seen: set[str] = set()

        for raw in self._listings(ctx, now):
            tally.raw_files += 1
            try:
                listing = AnnouncementsListingResponse.model_validate_json(ctx.blob.get(raw.content_hash))
            except (KeyError, BlobIntegrityError, BotoCoreError, ClientError) as exc:
                tally.problems.append(f"raw file {raw.content_hash[:12]} unreadable: {type(exc).__name__}: {exc}")
                continue
            except ValidationError as exc:
                tally.problems.append(f"raw file {raw.content_hash[:12]} is not a listing: {exc.error_count()} errors")
                continue
            for row in listing.data:
                key = announcement_key(row)
                if key in seen:
                    continue
                seen.add(key)
                tally.rows_parsed += 1
                try:
                    self._handle(ctx, raw, row, key, now, tally)
                except order_wins.LookAhead as exc:
                    tally.problems.append(f"announcement {key[:12]}: {exc}")
                except SQLAlchemyError as exc:
                    tally.problems.append(f"database write error: {type(exc).__name__}: {exc}")
                    return tally.result(self.name)
        return tally.result(self.name)

    def _listings(self, ctx: AdapterContext, now) -> list[RawSourceFile]:
        return raw_source_files_as_of(ctx.session, extracted_by=NseAnnouncements.extracted_by(), as_of=now)

    def _handle(self, ctx, raw: RawSourceFile, row: AnnouncementRow, key: str, now, tally: Tally) -> None:
        prov = dict(
            as_of=row.announced_on,
            content_hash=raw.content_hash,
            source_url=raw.source_url,
            extracted_by=self.extracted_by(),
            model_version=None,
        )
        if _ISIN.match(row.isin) is None:
            self._quarantine(
                ctx, tally, prov, key, row, None, OrderWinQuarantineReason.INVALID_ISIN, row.isin,
                order_wins.RULE_VERSION,
            )
            return

        result = order_wins.extract_order_win(
            order_wins.Announcement(
                isin=row.isin,
                subject=row.subject,
                description=row.description,
                announced_on=row.announced_on,
                as_of=row.announced_on,
                source_url=raw.source_url,
            ),
            now,
        )
        if isinstance(result, order_wins.Quarantined):
            if result.reason is order_wins.QuarantineReason.NOT_AN_ORDER:
                tally.not_an_order += 1
                return
            self._quarantine(
                ctx, tally, prov, key, row, row.isin, OrderWinQuarantineReason(result.reason.value),
                result.quote, result.rule_version,
            )
            return

        stmt = (
            insert(OrderWinFact.__table__)
            .values(
                isin=result.isin,
                announcement_key=key,
                announced_on=result.announced_on,
                order_value_inr=result.order_value_inr,
                value_quote=result.value_quote,
                counterparty=result.counterparty,
                counterparty_quote=result.counterparty,  # the name is a verbatim substring of the text
                execution_months=result.execution_months,
                execution_end=result.execution_end,
                period_quote=result.period_quote,
                matched_phrase=result.matched_phrase,
                missing=list(result.missing),
                rule_version=result.rule_version,
                **prov,
            )
            .on_conflict_do_nothing(constraint="uq_order_win_announcement_rule")
            .returning(OrderWinFact.__table__.c.id)
        )
        if ctx.session.execute(stmt).scalar_one_or_none() is not None:
            tally.rows_written += 1
        else:
            tally.already_known += 1

    def _quarantine(
        self, ctx, tally: Tally, prov: dict, key: str, row: AnnouncementRow, isin: str | None,
        reason: OrderWinQuarantineReason, quote: str | None, rule_version: str,
    ) -> None:
        stmt = (
            insert(OrderWinQuarantine.__table__)
            .values(
                isin=isin,
                announcement_key=key,
                announced_on=row.announced_on,
                reason=reason,
                quote=quote,
                rule_version=rule_version,
                **prov,
            )
            .on_conflict_do_nothing(constraint="uq_order_win_quarantine_announcement_rule")
            .returning(OrderWinQuarantine.__table__.c.id)
        )
        if ctx.session.execute(stmt).scalar_one_or_none() is not None:
            tally.quarantined += 1
        else:
            tally.already_known += 1

    def check_shape(self, ctx: AdapterContext) -> None:
        """Canary: the newest stored listing still validates. No network, ever."""
        listings = self._listings(ctx, require_aware(ctx.now(), "now()"))
        if listings:
            AnnouncementsListingResponse.model_validate_json(ctx.blob.get(listings[-1].content_hash))
