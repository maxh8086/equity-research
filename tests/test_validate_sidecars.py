"""Sidecars for hand-saved Screener exports: derived by code, accepted by the drop adapter."""

import json
import os
from datetime import timedelta

import pytest

from core.db.pit import index_list_names_as_of
from core.timezones import IST
from ingest.base import RunStatus
from ingest.screener_export.adapters import ScreenerExportDrop
from tests.test_screener_export_adapters import EXPORTED, FIXTURE, LISTED, NAME, URL, VINATI, _ctx, _index_list
from validate.sidecars import Company, match_company, normalise_name, write_sidecars

VIN = Company(VINATI, "VINATIORGA", "Vinati Organics Ltd.")
RIL = Company("INE002A01018", "RELIANCE", "Reliance Industries Ltd.")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("VINATI ORGANICS LTD", "Vinati Organics Ltd."),
        ("Bajaj Holdings & Investment Ltd", "Bajaj Holdings & Investment Limited"),
        ("Mahindra & Mahindra Ltd.", "MAHINDRA AND MAHINDRA LTD"),
    ],
)
def test_names_normalise_alike(a, b):
    assert normalise_name(a) == normalise_name(b)


def test_ltd_is_dropped_only_as_a_suffix():
    assert normalise_name("Ltd Holdings Ltd") == "ltd holdings"


def test_match_by_workbook_name_or_file_name():
    assert match_company("download (3)", "VINATI ORGANICS LTD", [VIN, RIL]) == VIN
    assert match_company("vinatiorga", "Something Else", [VIN, RIL]) == VIN


def test_no_match_or_conflict_is_a_reason():
    assert "matches no sample company" in match_company("x", "Unknown Ltd", [VIN, RIL])
    assert "different companies" in match_company("RELIANCE", "VINATI ORGANICS LTD", [VIN, RIL])


def _save(folder, name="Vinati Organics.xlsx"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(FIXTURE.read_bytes())
    os.utime(path, (EXPORTED.timestamp(), EXPORTED.timestamp()))
    return path


def test_sidecar_from_workbook_and_file_time(tmp_path):
    path = _save(tmp_path)
    lines, failures = write_sidecars(tmp_path, [VIN, RIL], consolidated=True)
    assert failures == 0 and "wrote" in lines[0]
    meta = json.loads(path.with_name(path.name + ".meta.json").read_text())
    assert meta["source_url"] == URL and meta["isin"] == VINATI and meta["consolidation"] == "consolidated"
    assert meta["published_at"] == EXPORTED.astimezone(IST).isoformat()


def test_existing_sidecar_is_kept_and_unknown_company_is_rejected(tmp_path):
    path = _save(tmp_path)
    meta = path.with_name(path.name + ".meta.json")
    meta.write_text("{}")
    lines, failures = write_sidecars(tmp_path, [VIN], consolidated=True)
    assert failures == 0 and "kept" in lines[0] and meta.read_text() == "{}"

    other = _save(tmp_path / "b")
    lines, failures = write_sidecars(tmp_path / "b", [RIL], consolidated=True)
    assert failures == 1 and "REJECTED" in lines[0]
    assert not other.with_name(other.name + ".meta.json").exists()


def test_standalone_url(tmp_path):
    path = _save(tmp_path)
    write_sidecars(tmp_path, [VIN], consolidated=False)
    meta = json.loads(path.with_name(path.name + ".meta.json").read_text())
    assert meta["source_url"] == "https://www.screener.in/company/VINATIORGA/" and meta["consolidation"] == "standalone"


@pytest.mark.db
def test_the_drop_adapter_accepts_a_written_sidecar(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _save(tmp_path / NAME)
    write_sidecars(tmp_path / NAME, [VIN], consolidated=True)
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail


@pytest.mark.db
def test_list_names_as_of(session):
    _index_list(session, "VINATIORGA", VINATI)
    from core.db.models import IndexSnapshotConstituent

    digest = session.query(IndexSnapshotConstituent.content_hash).scalar()
    assert index_list_names_as_of(session, content_hash=digest, as_of=LISTED) == {VINATI: "C"}
    assert index_list_names_as_of(session, content_hash=digest, as_of=LISTED - timedelta(seconds=1)) == {}
