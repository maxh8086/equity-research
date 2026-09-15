"""Shared adapter base (CLAUDE.md "Data sources" and "Code conventions").

An adapter is one source. It declares:
- `name`: snake_case; its switch is EQUITY_SOURCE_<NAME>_ENABLED
- `source_class`: official_api | official_archive | web_scrape | manual_drop
- `target_stores`: the tables it writes

and implements `ingest` (fetch, store raw bytes, validate strictly, write) and
`check_shape` (the daily canary). A disabled adapter returns status
"disabled", not "failed": it writes nothing, nothing is substituted, and its
canary is skipped.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from core.blob import BlobStore
from core.config import Settings
from core.db.models import RawSourceFile
from core.sources import SourceClass
from core.timezones import require_aware
from ingest.schema import StrictModel

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DISABLED = "disabled"
    FAILED = "failed"


class CanaryStatus(StrEnum):
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class RunResult:
    adapter: str
    status: RunStatus
    detail: str = ""
    raw_files: int = 0
    rows_written: int = 0
    quarantined: int = 0


@dataclass(frozen=True)
class CanaryResult:
    adapter: str
    status: CanaryStatus
    detail: str = ""


@dataclass(frozen=True)
class RawDocument:
    """Bytes already saved to blob storage and recorded in `raw_source_file`."""

    data: bytes
    content_hash: str
    source_url: str
    as_of: datetime
    fetched_at: datetime
    media_type: str


@dataclass
class AdapterContext:
    session: Session
    blob: BlobStore
    settings: Settings
    now: Callable[[], datetime]  # injected clock; returns aware datetimes


class Adapter(ABC):
    name: ClassVar[str]
    source_class: ClassVar[SourceClass]
    target_stores: ClassVar[tuple[str, ...]]

    @classmethod
    def extracted_by(cls) -> str:
        return f"{cls.__module__}.{cls.__qualname__}"

    @classmethod
    def switch_name(cls) -> str:
        return f"EQUITY_SOURCE_{cls.name.upper()}_ENABLED"

    @classmethod
    def disabled_reason(cls, settings: Settings) -> str | None:
        if cls.source_class is SourceClass.WEB_SCRAPE and not settings.web_scraping_enabled:
            return "EQUITY_WEB_SCRAPING_ENABLED is false"
        if not settings.source_switches.get(cls.name, False):
            return f"{cls.switch_name()} is not true"
        return None

    def run(self, ctx: AdapterContext) -> RunResult:
        if reason := self.disabled_reason(ctx.settings):
            return RunResult(self.name, RunStatus.DISABLED, reason)
        return self.ingest(ctx)

    def canary(self, ctx: AdapterContext) -> CanaryResult:
        if reason := self.disabled_reason(ctx.settings):
            return CanaryResult(self.name, CanaryStatus.SKIPPED, reason)
        self.check_shape(ctx)
        return CanaryResult(self.name, CanaryStatus.PASSED)

    @abstractmethod
    def ingest(self, ctx: AdapterContext) -> RunResult:
        """Fetch, `store_raw` before parsing, validate with a StrictModel, write the target stores."""

    @abstractmethod
    def check_shape(self, ctx: AdapterContext) -> None:
        """Canary: fetch a small live sample and validate it with the strict model. Raise on drift."""

    def store_raw(
        self,
        ctx: AdapterContext,
        *,
        data: bytes,
        source_url: str,
        as_of: datetime,
        media_type: str,
    ) -> RawDocument:
        """Save raw bytes to blob storage, then record them. Call before parsing.

        `as_of` is when the source published the content, never the fetch time.
        """
        require_aware(as_of, "as_of")
        fetched_at = require_aware(ctx.now(), "now()")
        if as_of > fetched_at:
            raise ValueError(f"as_of {as_of} is after fetch time {fetched_at}")
        key = ctx.blob.put(data)
        ctx.session.add(
            RawSourceFile(
                fetched_at=fetched_at,
                byte_size=len(data),
                media_type=media_type,
                as_of=as_of,
                content_hash=key,
                source_url=source_url,
                extracted_by=self.extracted_by(),
                model_version=None,
            )
        )
        ctx.session.flush()
        return RawDocument(data, key, source_url, as_of, fetched_at, media_type)


# --------------------------------------------------------------------------- #
# Drop folder: the fallback when a source blocks access or has no archive
# --------------------------------------------------------------------------- #

META_SUFFIX = ".meta.json"


class DropFileError(Exception):
    pass


class DropFileMeta(StrictModel):
    """Sidecar `<file>.meta.json`, written by the person who downloaded the file.

    `published_at` is when the source published the file (R2), never when it
    was downloaded. For a date-only source use 23:59:59+05:30 on that date.
    """

    source_url: str
    published_at: AwareDatetime
    media_type: str


class DropFolderAdapter(Adapter):
    """Reads files downloaded by hand from <EQUITY_DROP_FOLDER>/<name>/.

    It shares its parser with the networked adapter for the same data but is
    switched separately. Every file needs a sidecar; a missing or malformed
    sidecar fails the run rather than guessing provenance.
    """

    source_class = SourceClass.MANUAL_DROP

    def drop_dir(self, ctx: AdapterContext) -> Path:
        if ctx.settings.drop_folder is None:
            raise DropFileError("EQUITY_DROP_FOLDER is not set")
        folder = ctx.settings.drop_folder / self.name
        if not folder.is_dir():
            raise DropFileError(f"drop folder {folder} does not exist")
        return folder

    def pending(self, ctx: AdapterContext) -> list[tuple[Path, DropFileMeta]]:
        files = sorted(
            p for p in self.drop_dir(ctx).iterdir() if p.is_file() and not p.name.startswith(".")
        )
        data_files = [p for p in files if not p.name.endswith(META_SUFFIX)]
        sidecars = {p for p in files if p.name.endswith(META_SUFFIX)}
        out = []
        for path in data_files:
            meta_path = path.with_name(path.name + META_SUFFIX)
            if meta_path not in sidecars:
                raise DropFileError(f"{path.name} has no {meta_path.name}")
            sidecars.discard(meta_path)
            out.append((path, DropFileMeta.model_validate_json(meta_path.read_bytes())))
        if sidecars:
            raise DropFileError(f"sidecars without a file: {sorted(p.name for p in sidecars)}")
        return out

    def drop_files(self, ctx: AdapterContext) -> Iterator[RawDocument]:
        for path, meta in self.pending(ctx):
            yield self.store_raw(
                ctx,
                data=path.read_bytes(),
                source_url=meta.source_url,
                as_of=meta.published_at,
                media_type=meta.media_type,
            )

    def check_shape(self, ctx: AdapterContext) -> None:
        self.pending(ctx)
