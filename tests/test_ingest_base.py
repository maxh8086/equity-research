"""Adapter switches, registry and startup checks. No database needed."""

import pytest

from core.config import Settings
from core.sources import DeploymentMode, SourceClass
from ingest import __main__ as cli
from ingest.base import Adapter, AdapterContext, CanaryStatus, RunResult, RunStatus
from ingest.registry import (
    AdapterDeclarationError,
    StartupRefused,
    check_startup,
    discover,
    index_adapters,
    source_class_by_extracted_by,
    validate_declaration,
)
from tests.fakes import MemoryBlobStore


class _Recording(Adapter):
    def __init__(self) -> None:
        self.ingested = 0
        self.checked = 0

    def ingest(self, ctx):
        self.ingested += 1
        return RunResult(self.name, RunStatus.SUCCEEDED)

    def check_shape(self, ctx):
        self.checked += 1


class FakeScrape(_Recording):
    name = "fake_scrape"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = ("index_membership",)


class FakeApi(_Recording):
    name = "fake_api"
    source_class = SourceClass.OFFICIAL_API
    target_stores = ("price_daily",)


ADAPTERS = {"fake_scrape": FakeScrape, "fake_api": FakeApi}


def _ctx(**settings) -> AdapterContext:
    return AdapterContext(
        session=None,  # disabled adapters never touch it; fakes don't either
        blob=MemoryBlobStore(),
        settings=Settings(**settings),
        now=lambda: pytest.fail("clock read"),
    )


# --------------------------------------------------------------------------- #
# Switches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"web_scraping_enabled": False, "source_switches": {"fake_scrape": True}}, "EQUITY_WEB_SCRAPING_ENABLED"),
        ({"web_scraping_enabled": True, "source_switches": {"fake_scrape": False}}, "EQUITY_SOURCE_FAKE_SCRAPE_ENABLED"),
        ({"web_scraping_enabled": True, "source_switches": {}}, "EQUITY_SOURCE_FAKE_SCRAPE_ENABLED"),
    ],
)  # fmt: skip
def test_web_scrape_adapter_disabled_unless_both_switches_on(settings, reason):
    adapter = FakeScrape()
    result = adapter.run(_ctx(**settings))
    assert result.status is RunStatus.DISABLED
    assert reason in result.detail
    assert adapter.ingested == 0


def test_web_scrape_adapter_runs_with_both_switches_on():
    adapter = FakeScrape()
    ctx = _ctx(web_scraping_enabled=True, source_switches={"fake_scrape": True})
    assert adapter.run(ctx).status is RunStatus.SUCCEEDED
    assert adapter.ingested == 1


def test_master_scraping_switch_does_not_affect_official_sources():
    adapter = FakeApi()
    ctx = _ctx(web_scraping_enabled=False, source_switches={"fake_api": True})
    assert adapter.run(ctx).status is RunStatus.SUCCEEDED


def test_adapter_without_a_switch_is_disabled():
    assert FakeApi().run(_ctx()).status is RunStatus.DISABLED


def test_canary_skipped_when_disabled_and_run_when_enabled():
    adapter = FakeApi()
    assert adapter.canary(_ctx()).status is CanaryStatus.SKIPPED
    assert adapter.checked == 0
    assert adapter.canary(_ctx(source_switches={"fake_api": True})).status is CanaryStatus.PASSED
    assert adapter.checked == 1


def test_extracted_by_is_the_adapter_import_path():
    assert FakeApi.extracted_by() == "tests.test_ingest_base.FakeApi"
    assert source_class_by_extracted_by(ADAPTERS) == {
        "tests.test_ingest_base.FakeScrape": SourceClass.WEB_SCRAPE,
        "tests.test_ingest_base.FakeApi": SourceClass.OFFICIAL_API,
    }


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #


def _declared(**attrs) -> type[Adapter]:
    base = {"name": "x", "source_class": SourceClass.OFFICIAL_ARCHIVE, "target_stores": ("t",)}
    return type("Declared", (_Recording,), base | attrs)


@pytest.mark.parametrize(
    "attrs",
    [
        {"name": "NSE-Indices"},
        {"name": ""},
        {"source_class": "web_scrape"},
        {"target_stores": "index_membership"},
        {"target_stores": ()},
        {"target_stores": ("",)},
    ],
)
def test_invalid_declarations_rejected(attrs):
    with pytest.raises(AdapterDeclarationError):
        validate_declaration(_declared(**attrs))


def test_duplicate_adapter_names_rejected():
    with pytest.raises(AdapterDeclarationError, match="two adapters"):
        index_adapters([_declared(), _declared()])


def test_discovered_adapters_are_valid():
    adapters = discover()
    assert all(cls.__module__.startswith("ingest.") for cls in adapters.values())


def test_every_discovered_web_scrape_adapter_is_disabled_by_its_switch():
    ctx = _ctx(web_scraping_enabled=True)
    for name, cls in discover().items():
        if cls.source_class is SourceClass.WEB_SCRAPE:
            assert cls().run(ctx).status is RunStatus.DISABLED, name


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "settings",
    [
        {"deployment_mode": DeploymentMode.COMMERCIAL, "web_scraping_enabled": True},
        # Refused even with the master switch off: the flag shows intent.
        {"deployment_mode": DeploymentMode.COMMERCIAL, "source_switches": {"fake_scrape": True}},
        {"source_switches": {"fake_scrpe": True}},  # typo: would silently never run
    ],
)
def test_startup_refused(settings):
    with pytest.raises(StartupRefused):
        check_startup(Settings(**settings), ADAPTERS)


@pytest.mark.parametrize(
    "settings",
    [
        {"deployment_mode": DeploymentMode.COMMERCIAL, "source_switches": {"fake_api": True}},
        {"web_scraping_enabled": True, "source_switches": {"fake_scrape": True}},
    ],
)
def test_startup_allowed(settings):
    check_startup(Settings(**settings), ADAPTERS)


def test_cli_refuses_commercial_mode_with_scraping(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EQUITY_DEPLOYMENT_MODE", "commercial")
    monkeypatch.setenv("EQUITY_WEB_SCRAPING_ENABLED", "true")
    assert cli.main(["list"]) == 2
    assert "refusing to start" in capsys.readouterr().err
