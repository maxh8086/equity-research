"""Shared NSE session handling: cookie warm-up, client identification, throttling.

For all NSE API adapters (announcements, shareholding listing, etc.) to avoid
rate-limiting or blocking. Warm up cookies on the NSE homepage before making API calls,
identify the client with an honest User-Agent, and throttle requests politely.

This module is reused by:
- Session 7c: announcements adapter
- Session 7d: shareholding listing adapter
"""

from __future__ import annotations

import httpx

from ingest.http import PoliteClient


def warm_up_nse_session(client: PoliteClient, home_url: str = "https://www.nseindia.com") -> None:
    """Warm up NSE session by fetching the homepage to establish cookies.

    NSE returns 429 (Too Many Requests) without a valid session. A simple GET
    on the homepage establishes cookies that persist for subsequent API calls.

    Raises AccessBlocked if the homepage itself is blocked.
    Raises httpx.HTTPError if the request fails.
    """
    client.get(home_url)


def create_nse_client(
    user_agent: str,
    min_interval_seconds: float,
    transport: httpx.BaseTransport | None = None,
) -> PoliteClient:
    """Create a PoliteClient pre-configured for NSE requests.

    NSE requires:
    - An honest User-Agent (not impersonated)
    - Minimum interval between requests
    - Cookie persistence (maintained by httpx.Client)
    - robots.txt is not authoritative for NSE's API endpoints
    """
    return PoliteClient(
        user_agent=user_agent,
        min_interval_seconds=min_interval_seconds,
        respect_robots=False,  # NSE's API endpoints are not governed by robots.txt
        timeout_seconds=30.0,
        overload_retries=0,  # Never retry on overload (502/503/504); fall back instead
        transport=transport,
    )
