"""Screener exports in the database: drop-folder adapter, identity checks at `as_of`, revisions, reparse."""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    Consolidation,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    ScreenerExport,
    ScreenerValue,
)
from core.db.pit import screener_exports_as_of, screener_values_as_of
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.screener_export import adapters
from ingest.screener_export.adapters import XLSX, ScreenerExportDrop
from ingest.screener_export.parser import CRORE, RULE_VERSION
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

FIXTURE = Path(__file__).parent / "fixtures" / "screener_export" / "vinati_data_sheet_v2_1.xlsx"
NAME = "screener_export_drop"
VINATI, OTHER = "INE410B01037", "INE002A01018"
URL = "https://www.screener.in/company/VINATIORGA/consolidated/"
EXPORTED = datetime(2022, 11, 20, 10, 0, tzinfo=IST)
LISTED = datetime(2022, 9, 30, 23, 59, 59, tzinfo=IST)
C = Consolidation.CONSOLIDATED


def _ctx(session, tmp_path, now, blob=None) -> AdapterContext:
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: True})
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _drop(tmp_path: Path, published_at=EXPORTED, *, url=URL, isin=VINATI, consolidation="consolidated",
          name="vinati.xlsx", data=None) -> None:  # fmt: skip
    folder = tmp_path / NAME
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data if data is not None else FIXTURE.read_bytes())
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": url, "published_at": published_at.isoformat(), "media_type": XLSX,
                    "isin": isin, "consolidation": consolidation})  # fmt: skip
    )


def _index_list(session, symbol: str, isin: str, published: datetime = LISTED) -> None:
    prov = dict(as_of=published, content_hash=content_hash(f"{symbol}{isin}{published}".encode()),
                source_url=f"list-{published}", extracted_by="tests", model_version=None)  # fmt: skip
    snapshot = IndexSnapshot(index_code=IndexCode.NIFTY_NEXT_50, constituent_count=1, quarantined_rows=0,
                             rule_version="t", **prov)  # fmt: skip
    session.add(snapshot)
    session.flush()
    session.add(
        IndexSnapshotConstituent(snapshot_id=snapshot.id, isin=isin, symbol=symbol, series="EQ", company_name="C",
                                 industry="I", row_number=1, **prov)  # fmt: skip
    )
    session.flush()


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_export_loads_in_absolute_units(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert result.rows_written == 431

    export, values = screener_values_as_of(session, isin=VINATI, consolidation=C, as_of=EXPORTED,
                                           rule_version=RULE_VERSION)  # fmt: skip
    assert export is not None and export.company_name == "VINATI ORGANICS LTD"
    assert len(values) == 431
    sales = {v.period_end: v.value for v in values if (v.statement, v.line) == ("pl", "sales")}
    assert sales[date(2022, 3, 31)] == Decimal("1615.51") * CRORE
    # Nothing is visible before the export time.
    assert screener_values_as_of(session, isin=VINATI, consolidation=C, as_of=EXPORTED - timedelta(seconds=1),
                                 rule_version=RULE_VERSION) == (None, [])  # fmt: skip
    # Standalone is another view: absent.
    assert screener_values_as_of(session, isin=VINATI, consolidation=Consolidation.STANDALONE, as_of=EXPORTED,
                                 rule_version=RULE_VERSION)[0] is None  # fmt: skip


def test_second_run_loads_nothing_new(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=2)))
    assert result.status is RunStatus.SUCCEEDED and "1 files already loaded" in result.detail
    assert _count(session, ScreenerExport) == 1 and _count(session, ScreenerValue) == 431


def test_newest_export_known_at_t_wins(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))

    later = EXPORTED + timedelta(days=30)
    wb = load_workbook(FIXTURE)
    wb["Data Sheet"]["K17"] = 1700
    out = BytesIO()
    wb.save(out)
    _drop(tmp_path, later, data=out.getvalue())
    assert ScreenerExportDrop().run(_ctx(session, tmp_path, later)).status is RunStatus.SUCCEEDED

    def fy22_sales(as_of):
        _, values = screener_values_as_of(session, isin=VINATI, consolidation=C, as_of=as_of,
                                          rule_version=RULE_VERSION)  # fmt: skip
        return next(v.value for v in values if (v.line, v.period_end) == ("sales", date(2022, 3, 31)))

    assert fy22_sales(EXPORTED + timedelta(days=1)) == Decimal("1615.51") * CRORE
    assert fy22_sales(later) == 1700 * CRORE
    assert len(screener_exports_as_of(session, as_of=later, isin=VINATI)) == 2


@pytest.mark.parametrize(
    "drop, message",
    [
        (dict(url="https://www.screener.in/company/VINATIORGA/"), "does not match consolidation"),
        (dict(url="https://screener.in/company/VINATIORGA/consolidated/"), "not a Screener company page"),
        (dict(isin=OTHER), "the sidecar says INE002A01018"),
        (dict(url="https://www.screener.in/company/RELIANCE/consolidated/"), r"constituent lists give \[\]"),
        (dict(published_at=datetime(2022, 9, 30, 23, 0, tzinfo=IST)), "not before the export date"),
    ],
)
def test_identity_and_period_problems_reject_the_file(session, tmp_path, drop, message):
    _index_list(session, "VINATIORGA", VINATI, datetime(2022, 9, 1, tzinfo=IST))
    _drop(tmp_path, **drop)
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    assert result.status is RunStatus.FAILED
    assert "1 files rejected" in result.detail
    assert re.search(message, result.detail), result.detail
    assert _count(session, ScreenerExport) == 0 and _count(session, ScreenerValue) == 0


def test_symbol_listed_only_after_export_is_rejected(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI, EXPORTED + timedelta(days=1))
    _drop(tmp_path)
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(days=2)))
    assert result.status is RunStatus.FAILED and "constituent lists give []" in result.detail


def test_reparse_rebuilds_from_blob_under_a_new_rule_version(session, tmp_path, monkeypatch):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    blob = MemoryBlobStore()
    now = EXPORTED + timedelta(hours=1)
    ScreenerExportDrop().run(_ctx(session, tmp_path, now, blob))
    for p in (tmp_path / NAME).iterdir():
        p.unlink()

    monkeypatch.setattr(adapters, "RULE_VERSION", "screener_export/test")
    result = ScreenerExportDrop().reparse(_ctx(session, tmp_path, now, blob))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert result.rows_written == 431
    export, values = screener_values_as_of(session, isin=VINATI, consolidation=C, as_of=now,
                                           rule_version="screener_export/test")  # fmt: skip
    assert export is not None and len(values) == 431


def test_rows_are_append_only(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(ScreenerValue.__table__.update().values(value=0))
