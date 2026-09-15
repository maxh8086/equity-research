"""HTTP for every networked adapter: identified, throttled, never escalating.

CLAUDE.md "Breakage": when access is blocked (401/403/429/451, or robots.txt
disallows the URL) the client stops and raises `AccessBlocked`. It does not
retry, rotate, impersonate or slow-poll around the block; the fallback is the
source's drop-folder adapter.
"""

import time
import urllib.robotparser
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import httpx

BLOCK_STATUSES = frozenset({401, 403, 429, 451})


class AccessBlocked(Exception):
    """The source refused us. Do not work around it; use the drop folder."""


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_seconds: float,
        respect_robots: bool,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`respect_robots` is True for websites and archives; official APIs are governed by their terms."""
        self._user_agent = user_agent
        self._min_interval = min_interval_seconds
        self._respect_robots = respect_robots
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        if self._respect_robots and not self._robots_for(url).can_fetch(self._user_agent, url):
            raise AccessBlocked(f"robots.txt disallows {url}")
        response = self._throttled_get(url, params=params, headers=headers)
        if response.status_code in BLOCK_STATUSES:
            raise AccessBlocked(f"{url} answered HTTP {response.status_code}")
        response.raise_for_status()
        return response

    def _throttled_get(self, url: str, **kwargs) -> httpx.Response:
        host = urlsplit(url).netloc
        if (last := self._last_request.get(host)) is not None:
            wait = self._min_interval - (self._monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        try:
            return self._http.get(url, **kwargs)
        finally:
            self._last_request[host] = self._monotonic()

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser(f"{origin}/robots.txt")
            response = self._throttled_get(f"{origin}/robots.txt")
            if response.status_code in BLOCK_STATUSES:
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True  # no robots.txt
            else:
                parser.parse(response.text.splitlines())
            self._robots[origin] = parser
        return self._robots[origin]
