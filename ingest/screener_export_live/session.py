"""One logged-in Screener session: identified, throttled, never escalating.

Unlike `ingest.http.PoliteClient` (GET only), this client also sends the login
and export POSTs and keeps one cookie jar. It follows the same rules:

- one `httpx.Client`, so one session and one honest User-Agent;
- never two requests closer than `min_interval_seconds` (redirect hops, the
  robots.txt fetch and retries all count; the caller passes a value that is
  already at or above the floor);
- 401/403/429/451, or a robots.txt disallow, raise `AccessBlocked` at once and
  are never retried, rotated or worked around;
- a redirect to /login/ raises `LoginRedirect` unless the caller expects the
  login page. The session is never silently re-established;
- an overloaded server (502/503/504) or a timeout gets exactly one retry after
  a polite backoff, at least as long as any `Retry-After` header asks.

Redirects are followed by hand so each hop is throttled and inspected.
Credentials are passed by the caller as form data; nothing here logs, stores or
puts them in a message.
"""

from __future__ import annotations

import time
import urllib.robotparser
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from ingest.http import BLOCK_STATUSES, OVERLOAD_STATUSES, AccessBlocked

LOGIN_PATH = "/login/"
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class LoginRedirect(Exception):
    """A request was redirected to the login page: the session is gone. Stop the run."""


def _retry_after(response: httpx.Response) -> float:
    """Seconds from a numeric Retry-After header; 0 when absent or given as a date."""
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else 0.0


class ScreenerSession:
    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_seconds: float,
        backoff_seconds: float,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user_agent = user_agent
        self._min_interval = min_interval_seconds
        self._backoff = backoff_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: float | None = None
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> ScreenerSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def cookie(self, name: str) -> str | None:
        return self._http.cookies.get(name)

    def get(self, url: str, *, allow_login_page: bool = False) -> httpx.Response:
        return self._send("GET", url, allow_login_page=allow_login_page)

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        referer: str,
        follow: bool = True,
        allow_login_page: bool = False,
    ) -> httpx.Response:
        """POST a form. With `follow=False` the first response is returned even if it is a redirect."""
        return self._send(
            "POST", url, data=data, headers={"Referer": referer}, follow=follow, allow_login_page=allow_login_page
        )

    # ------------------------------------------------------------------ #

    def _send(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        follow: bool = True,
        allow_login_page: bool = False,
    ) -> httpx.Response:
        for _ in range(MAX_REDIRECTS + 1):
            self._check_robots(url)
            response = self._attempt(method, url, data=data, headers=headers)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                target = str(httpx.URL(url).join(location))
                if not allow_login_page and urlsplit(target).path.startswith(LOGIN_PATH):
                    raise LoginRedirect(f"{urlsplit(url).path} redirected back to the login page")
                if not follow:
                    return response
                url = target
                if response.status_code in (301, 302, 303):
                    method, data, headers = "GET", None, None  # a browser turns the redirected POST into a GET
                continue
            return response
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects from {urlsplit(url).path}")

    def _attempt(
        self, method: str, url: str, *, data: Mapping[str, str] | None, headers: Mapping[str, str] | None
    ) -> httpx.Response:
        """One request with at most one polite retry on an overloaded server or a timeout."""
        for attempt in (1, 2):
            last = attempt == 2
            try:
                response = self._throttled(method, url, data=data, headers=headers)
            except httpx.TimeoutException:
                if last:
                    raise
                self._sleep(self._backoff)
                continue
            if response.status_code in BLOCK_STATUSES:
                raise AccessBlocked(f"{urlsplit(url).path} answered HTTP {response.status_code}")
            if response.status_code in OVERLOAD_STATUSES and not last:
                self._sleep(max(self._backoff, _retry_after(response)))
                continue
            if response.status_code >= 400:
                response.raise_for_status()
            return response
        raise AssertionError("unreachable")

    def _throttled(
        self, method: str, url: str, *, data: Mapping[str, str] | None, headers: Mapping[str, str] | None
    ) -> httpx.Response:
        if self._last_request is not None:
            wait = self._min_interval - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        try:
            return self._http.request(method, url, data=data, headers=headers)
        finally:
            self._last_request = self._monotonic()

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser(f"{origin}/robots.txt")
            response = self._throttled("GET", f"{origin}/robots.txt", data=None, headers=None)
            if response.status_code in BLOCK_STATUSES:
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True  # no robots.txt
            else:
                parser.parse(response.text.splitlines())
            self._robots[origin] = parser
        if not self._robots[origin].can_fetch(self._user_agent, url):
            raise AccessBlocked(f"robots.txt disallows {parts.path}")
