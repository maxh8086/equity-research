"""Source classes and deployment modes (CLAUDE.md "Data sources").

Lives in core/ rather than ingest/ so point-in-time reads can filter by
source class without importing adapters.
"""

from enum import StrEnum


class SourceClass(StrEnum):
    OFFICIAL_API = "official_api"
    OFFICIAL_ARCHIVE = "official_archive"
    WEB_SCRAPE = "web_scrape"
    MANUAL_DROP = "manual_drop"


class DeploymentMode(StrEnum):
    PERSONAL = "personal"
    COMMERCIAL = "commercial"
