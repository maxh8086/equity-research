"""HTTP for every networked adapter: identified, throttled, never escalating.

CLAUDE.md "Breakage": when access is blocked (401/403/429/451, or robots.txt
disallows the URL) the client stops and raises `AccessBlocked`. It does not
retry, rotate, impersonate or slow-poll around the block; the fallback is the
source's drop-folder adapter.

An overloaded server is not a block. A caller may opt in to a bounded number
of retries on 502/503/504 and timeouts, waiting longer each time and at least
as long as a `Retry-After` header asks. A block is never retried.
"""

import gzip
import time
import urllib.robotparser
import zlib
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import httpx

BLOCK_STATUSES = frozenset({401, 403, 429, 451})
OVERLOAD_STATUSES = frozenset({502, 503, 504})
_TRANSFER_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


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
        overload_retries: int = 0,
        retry_backoff_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`respect_robots` is True for websites and archives; official APIs are governed by their terms.

        `overload_retries` extra attempts follow a 502/503/504 or timeout, the
        n-th after n × `retry_backoff_seconds`.
        """
        self._user_agent = user_agent
        self._min_interval = min_interval_seconds
        self._respect_robots = respect_robots
        self._retries = overload_retries
        self._backoff = retry_backoff_seconds
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
        return self._get(url, params=params, headers=headers, keep_raw=False)[0]

    def get_with_raw(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[httpx.Response, bytes]:
        """The response (content decoded) and its body exactly as transferred.

        For checking a digest an archive computed over the stored payload,
        which may be gzip-encoded.
        """
        response, raw = self._get(url, params=params, headers=headers, keep_raw=True)
        assert raw is not None
        return response, raw

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        keep_raw: bool,
    ) -> tuple[httpx.Response, bytes | None]:
        if self._respect_robots and not self._robots_for(url).can_fetch(self._user_agent, url):
            raise AccessBlocked(f"robots.txt disallows {url}")
        for attempt in range(1, self._retries + 2):
            last = attempt > self._retries
            try:
                response, raw = self._throttled_get(
                    url, params=params, headers=headers, keep_raw=keep_raw
                )
            except httpx.TimeoutException:
                if last:
                    raise
                self._sleep(self._backoff * attempt)
                continue
            if response.status_code in BLOCK_STATUSES:
                raise AccessBlocked(f"{url} answered HTTP {response.status_code}")
            if response.status_code in OVERLOAD_STATUSES and not last:
                self._sleep(max(self._backoff * attempt, _retry_after(response)))
                continue
            response.raise_for_status()
            return response, raw
        raise AssertionError("unreachable")

    def _throttled_get(
        self, url: str, *, keep_raw: bool = False, **kwargs
    ) -> tuple[httpx.Response, bytes | None]:
        host = urlsplit(url).netloc
        if (last := self._last_request.get(host)) is not None:
            wait = self._min_interval - (self._monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        try:
            if not keep_raw:
                return self._http.get(url, **kwargs), None
            streamed = self._http.send(self._http.build_request("GET", url, **kwargs), stream=True)
            try:
                raw = b"".join(streamed.iter_raw())
            finally:
                streamed.close()
            decoded = httpx.Response(
                streamed.status_code,
                headers=[
                    (k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _TRANSFER_HEADERS
                ],
                content=_decode(raw, streamed.headers.get("content-encoding", "")),
                request=streamed.request,
                history=streamed.history,
            )
            return decoded, raw
        finally:
            self._last_request[host] = self._monotonic()

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser(f"{origin}/robots.txt")
            response, _ = self._throttled_get(f"{origin}/robots.txt")
            if response.status_code in BLOCK_STATUSES:
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True  # no robots.txt
            else:
                parser.parse(response.text.splitlines())
            self._robots[origin] = parser
        return self._robots[origin]


def _retry_after(response: httpx.Response) -> float:
    """Seconds from a numeric Retry-After header; 0 when absent or given as a date."""
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else 0.0


def _decode(raw: bytes, content_encoding: str) -> bytes:
    """Undo Content-Encoding (gzip, deflate), applied in the order listed."""
    data = raw
    for coding in reversed([c.strip().lower() for c in content_encoding.split(",") if c.strip()]):
        if coding == "identity":
            continue
        if coding in ("gzip", "x-gzip"):
            data = gzip.decompress(data)
        elif coding == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        else:
            raise ValueError(f"unsupported Content-Encoding {coding!r}")
    return data
