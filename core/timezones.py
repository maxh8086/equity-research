from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def require_aware(value: datetime, name: str = "timestamp") -> datetime:
    """Reject naive datetimes. A naive as_of silently takes the server's zone."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {value!r}")
    return value
