import pytest
from pydantic import ValidationError

from core.config import get_settings, parse_source_switches
from core.sources import DeploymentMode


def test_source_switches_parsed_from_env_names():
    switches = parse_source_switches(
        {
            "EQUITY_SOURCE_NSE_BHAVCOPY_ENABLED": "true",
            "EQUITY_SOURCE_UPSTOX_ENABLED": "0",
            "EQUITY_WEB_SCRAPING_ENABLED": "true",
            "OTHER_SOURCE_X_ENABLED": "true",
        }
    )
    assert switches == {"nse_bhavcopy": True, "upstox": False}


def test_source_switch_names_are_case_insensitive():
    # Windows environment variable names are case-insensitive.
    assert parse_source_switches({"equity_source_upstox_enabled": "yes"}) == {"upstox": True}


def test_non_boolean_switch_value_fails_loudly():
    with pytest.raises(ValidationError):
        parse_source_switches({"EQUITY_SOURCE_UPSTOX_ENABLED": "maybe"})


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env
    for key in list(__import__("os").environ):
        if key.upper().startswith("EQUITY_"):
            monkeypatch.delenv(key)
    return monkeypatch


def test_defaults_are_personal_with_everything_off(clean_env):
    settings = get_settings()
    assert settings.deployment_mode is DeploymentMode.PERSONAL
    assert settings.web_scraping_enabled is False
    assert settings.source_switches == {}


def test_settings_read_switches_from_environment_and_env_file(clean_env, tmp_path):
    (tmp_path / ".env").write_text("EQUITY_SOURCE_NSE_INDICES_ENABLED=true\n")
    clean_env.setenv("EQUITY_DEPLOYMENT_MODE", "commercial")
    clean_env.setenv("EQUITY_SOURCE_UPSTOX_ENABLED", "true")
    settings = get_settings()
    assert settings.deployment_mode is DeploymentMode.COMMERCIAL
    assert settings.source_switches == {"nse_indices": True, "upstox": True}


def test_environment_overrides_env_file(clean_env, tmp_path):
    (tmp_path / ".env").write_text("EQUITY_SOURCE_UPSTOX_ENABLED=true\n")
    clean_env.setenv("EQUITY_SOURCE_UPSTOX_ENABLED", "false")
    assert get_settings().source_switches == {"upstox": False}
