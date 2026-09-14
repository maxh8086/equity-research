"""Declarative base and the provenance columns every table carries (R2).

`as_of` semantics: the moment the information became publicly knowable
(e.g. exchange dissemination time of the filing), NOT the time we ingested it.
Loading a 2019 annual report today stores its 2019 publication time in
`as_of` and today in `ingested_at`. That is recording history, not
backfilling it. What R2 forbids is inventing knowledge times for things the
system itself derived (e.g. `first_detected_on`) or revising a stored value.

Stores are append-only: a restatement is a new row with a later `as_of`.
Reads pick the latest row with `as_of <= t` (see core.db.pit).
"""

from datetime import datetime

from sqlalchemy import CHAR, DateTime, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from core.timezones import require_aware


class Base(DeclarativeBase):
    pass


class ProvenanceMixin:
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_by: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @validates("as_of")
    def _validate_as_of(self, key: str, value: datetime) -> datetime:
        return require_aware(value, key)
