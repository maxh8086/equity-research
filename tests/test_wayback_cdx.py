"""Contract tests for Wayback CDX parsing and capture selection. No services needed."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.nse_indices.adapters import CdxCapture, CdxShapeError, parse_cdx, run_boundaries, wayback_digest

FIXTURES = Path(__file__).parent / "fixtures" / "nse_indices"


def recorded_cdx() -> list[list[str]]:
    """The recorded response, minus the `length` column the adapter no longer requests."""
    rows = json.loads((FIXTURES / "cdx_niftyindices_ind_nifty50list.json").read_bytes())
    return [row[:5] for row in rows]


def test_recorded_cdx_response_parses():
    captures = parse_cdx(json.dumps(recorded_cdx()).encode())
    assert len(captures) >= 6
    assert captures[0].captured_at == datetime(2017, 10, 16, 8, 31, 42, tzinfo=timezone.utc)
    assert captures[0].digest == "KH6SOBBVQ5CRSCCU6GDS4CPXBSCMCSXM"


def test_empty_cdx_response():
    assert parse_cdx(b"[]") == []


@pytest.mark.parametrize(
    "rows",
    [
        [["timestamp", "original"], ["20171016083142", "x"]],
        [recorded_cdx()[0], recorded_cdx()[1][:4]],
        [recorded_cdx()[0], [*recorded_cdx()[1][:3], "302", recorded_cdx()[1][4]]],
        {"captures": []},
    ],
    ids=["header-changed", "short-row", "non-200", "not-a-list"],
)
def test_cdx_shape_change_fails_loudly(rows):
    with pytest.raises((CdxShapeError, ValueError)):
        parse_cdx(json.dumps(rows).encode())


def _capture(timestamp: str, digest: str) -> CdxCapture:
    return CdxCapture(timestamp=timestamp, original="x", mimetype="text/csv", statuscode="200", digest=digest * 32)


def test_run_boundaries_keeps_first_and_last_of_each_identical_run():
    captures = [
        _capture("20200105000000", "B"),
        _capture("20200101000000", "A"),
        _capture("20200102000000", "A"),
        _capture("20200103000000", "A"),
        _capture("20200106000000", "A"),
    ]
    assert [c.timestamp for c in run_boundaries(captures)] == [
        "20200101000000", "20200103000000", "20200105000000", "20200106000000"
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        ("ind_nifty50list_20160310050302.csv", "X4NQ6AAHLIWVUEQQITE4FUJ4YWIECV2V"),
        ("ind_nifty50list_20171016083142.csv", "KH6SOBBVQ5CRSCCU6GDS4CPXBSCMCSXM"),
    ],
)
def test_recorded_captures_match_their_archive_digests(name, digest):
    assert wayback_digest((FIXTURES / name).read_bytes()) == digest
