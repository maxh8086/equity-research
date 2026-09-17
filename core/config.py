import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.sources import DeploymentMode

ENV_FILE = ".env"
_SOURCE_SWITCH = re.compile(r"^EQUITY_SOURCE_(?P<name>[A-Z0-9_]+)_ENABLED$")
_BOOL = TypeAdapter(bool)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EQUITY_", env_file=ENV_FILE, extra="ignore")

    database_url: str = "postgresql+psycopg://equity:equity_local_dev@127.0.0.1:5433/equity"
    test_database_url: str = (
        "postgresql+psycopg://equity:equity_local_dev@127.0.0.1:5433/equity_test"
    )

    # Data-source switches (CLAUDE.md "Data sources"). Off unless turned on.
    deployment_mode: DeploymentMode = DeploymentMode.PERSONAL
    web_scraping_enabled: bool = False
    # EQUITY_SOURCE_<NAME>_ENABLED flags keyed by lowercase adapter name, filled
    # by get_settings(). An adapter with no flag is disabled.
    source_switches: dict[str, bool] = Field(default_factory=dict)

    # Raw source files: S3-compatible object storage (MinIO on the laptop).
    blob_endpoint_url: str = "http://127.0.0.1:9000"
    blob_region: str = "us-east-1"
    blob_access_key: str = "equity"
    blob_secret_key: SecretStr = SecretStr("equity_local_dev")
    blob_bucket: str = "equity-raw"
    test_blob_bucket: str = "equity-raw-test"

    # Files downloaded by hand, read by manual_drop adapters from <drop_folder>/<adapter name>/.
    drop_folder: Path | None = None

    # Every request identifies the client (CLAUDE.md "Code conventions": scraping).
    http_user_agent: str = "equity-knowledge/0.1 (private family research)"

    # NSE Indices constituent lists (ingest/nse_indices). niftyindices.com refuses
    # automated clients; NSE's archive host serves the same files.
    nse_indices_archive_base_url: str = "https://archives.nseindia.com/content/indices"
    nse_indices_min_interval_seconds: float = 3.0

    # Wayback Machine captures of those lists: one CDX query per location and file.
    wayback_cdx_url: str = "https://web.archive.org/cdx/search/cdx"
    wayback_capture_url: str = "https://web.archive.org/web/{timestamp}id_/{original}"
    wayback_min_interval_seconds: float = 10.0
    # CDX queries over broad locations (www/www1 variants) can take over a minute.
    wayback_timeout_seconds: float = 120.0
    # The CDX service is often overloaded (503/504, slow queues) without blocking
    # anyone: retry that a bounded number of times. A 429 or 403 still stops the run.
    wayback_overload_retries: int = 2
    wayback_retry_backoff_seconds: float = 60.0
    wayback_list_locations: list[str] = Field(
        default_factory=lambda: [
            "niftyindices.com/IndexConstituent",
            "nseindia.com/content/indices",
            "archives.nseindia.com/content/indices",
            "nsearchives.nseindia.com/content/indices",
        ]
    )

    # NSE bhavcopy (ingest/nse_bhavcopy): daily full capital-market UDiFF CSV, zipped,
    # one file per trade date. Unlike the indices lists, NSE's own archive host serves
    # the whole history back to the format's start (no Wayback needed).
    nse_bhavcopy_archive_url_template: str = (
        "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
    )
    nse_bhavcopy_min_interval_seconds: float = 3.0


def parse_source_switches(environ: Mapping[str, str]) -> dict[str, bool]:
    """EQUITY_SOURCE_<NAME>_ENABLED → {name: bool}. A value that is not a boolean raises."""
    switches = {}
    for key, raw in environ.items():
        if match := _SOURCE_SWITCH.match(key.upper()):
            switches[match["name"].lower()] = _BOOL.validate_python(raw.strip())
    return switches


def get_settings() -> Settings:
    environ = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}
    environ.update(os.environ)
    return Settings(source_switches=parse_source_switches(environ))
