"""Read-only broker holdings drop-folder adapter (CLAUDE.md "Integrations: brokers and MCP").

A person, or a separate read-only session, saves the JSON output of Zerodha
Kite `get_holdings` into <EQUITY_DROP_FOLDER>/broker_holdings_drop/. This
adapter only reads that file: it makes no network call and no MCP call, holds
no broker token, and has no way to place an order. The holdings feed
ownership rules and, later, `portfolio_risk_snapshot`; they are never a
source of prices, facts or membership.

- The sidecar names the account (`account_label`) and the time the broker
  reported the positions (`snapshot_at`). `published_at` is when the snapshot
  was known to this system and becomes `as_of`; it cannot precede `snapshot_at`,
  and neither may be in the future.
- Positions are keyed by ISIN. Records without a usable ISIN, with a negative
  or unparseable quantity or price, on an unknown exchange, or repeated, go to
  `broker_holding_quarantine`; the rest of the file is still stored.
- A file already loaded (same bytes) writes nothing on a rerun.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, StringConstraints

from core.compute.hashing import content_hash
from core.db.models import BrokerHoldingQuarantine, BrokerHoldingSnapshot
from core.db.pit import broker_holdings_file_loaded
from ingest.base import AdapterContext, DropFileMeta, DropFolderAdapter, RunResult, RunStatus
from ingest.broker_holdings_drop.parser import HoldingsRejected, parse_holdings

TARGET_STORES = ("broker_holding_snapshot", "broker_holding_quarantine")

AccountLabel = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(.*\S)?$")]


class BrokerHoldingsMeta(DropFileMeta):
    """Sidecar for a holdings dump: provenance plus which account, and when the broker reported it."""

    account_label: AccountLabel
    snapshot_at: AwareDatetime


@dataclass
class Tally:
    raw_files: int = 0
    rows_written: int = 0
    quarantined: int = 0
    already_loaded: int = 0
    rejected_files: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: it needs a person."""
        detail = (
            f"{self.rows_written} positions, {self.quarantined} quarantined, "
            f"{self.rejected_files} files rejected, {self.already_loaded} files already loaded"
        )
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        return RunResult(
            adapter,
            RunStatus.FAILED if self.rejected_files else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.rows_written,
            quarantined=self.quarantined,
        )


def _time_problem(meta: BrokerHoldingsMeta, now: datetime) -> str | None:
    if meta.snapshot_at > now:
        return f"snapshot_at {meta.snapshot_at.isoformat()} is in the future"
    if meta.published_at > now:
        return f"published_at {meta.published_at.isoformat()} is in the future"
    if meta.published_at < meta.snapshot_at:
        return "published_at is before snapshot_at: a snapshot cannot be known before it was taken"
    return None


class BrokerHoldingsDrop(DropFolderAdapter):
    """Kite `get_holdings` JSON saved into <EQUITY_DROP_FOLDER>/broker_holdings_drop/.

    Sidecar: `source_url` a label for where the dump came from (for example
    "manual://kite/get_holdings"), `published_at` when it was known (IST),
    `media_type` "application/json", `account_label`, and `snapshot_at`.
    """

    name = "broker_holdings_drop"
    target_stores = TARGET_STORES
    meta_model = BrokerHoldingsMeta

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        now = ctx.now()
        for path, meta in self.pending(ctx):
            assert isinstance(meta, BrokerHoldingsMeta)
            if problem := _time_problem(meta, now):
                tally.rejected_files += 1
                tally.problems.append(f"{path.name}: {problem}")
                continue
            data = path.read_bytes()
            if broker_holdings_file_loaded(ctx.session, content_hash=content_hash(data)):
                tally.already_loaded += 1
                continue
            doc = self.store_raw(
                ctx, data=data, source_url=meta.source_url, as_of=meta.published_at,
                media_type=meta.media_type,
            )  # fmt: skip
            tally.raw_files += 1
            try:
                parsed = parse_holdings(doc.data)
            except HoldingsRejected as exc:
                tally.rejected_files += 1
                tally.problems.append(f"{path.name} ({doc.content_hash[:12]}): {exc}")
                continue
            prov = dict(as_of=doc.as_of, content_hash=doc.content_hash, source_url=doc.source_url,
                        extracted_by=self.extracted_by(), model_version=None,
                        account_label=meta.account_label, snapshot_at=meta.snapshot_at)  # fmt: skip
            for row in parsed.rows:
                ctx.session.add(
                    BrokerHoldingSnapshot(isin=row.isin, exchange=row.exchange, quantity=row.quantity,
                                          average_price=row.average_price, last_price=row.last_price,
                                          **prov)  # fmt: skip
                )
            for q in parsed.quarantined:
                ctx.session.add(
                    BrokerHoldingQuarantine(isin=q.isin, row_number=q.row_number, reason=q.reason,
                                            detail=q.detail, **prov)  # fmt: skip
                )
            ctx.session.flush()
            tally.rows_written += len(parsed.rows)
            tally.quarantined += len(parsed.quarantined)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _ in self.pending(ctx):
            parse_holdings(path.read_bytes())
