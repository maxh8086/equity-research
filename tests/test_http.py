import httpx
import pytest

from ingest.http import AccessBlocked, PoliteClient

UA = "equity-knowledge/test"


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _client(routes: dict[str, httpx.Response], *, respect_robots=True, clock=None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(request.url.path, httpx.Response(404))

    clock = clock or FakeClock()
    client = PoliteClient(
        user_agent=UA,
        min_interval_seconds=2.0,
        respect_robots=respect_robots,
        transport=httpx.MockTransport(handler),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return client, seen


def test_identifies_itself():
    client, seen = _client({"/data.csv": httpx.Response(200, text="ok")}, respect_robots=False)
    client.get("https://example.in/data.csv")
    assert seen[0].headers["User-Agent"] == UA


def test_throttles_per_host():
    clock = FakeClock()
    client, _ = _client({"/a": httpx.Response(200), "/b": httpx.Response(200)}, respect_robots=False, clock=clock)
    client.get("https://example.in/a")
    clock.t += 0.5
    client.get("https://example.in/b")
    client.get("https://other.in/a")
    assert clock.sleeps == [1.5]


def test_robots_disallow_stops_before_requesting():
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
    client, seen = _client({"/robots.txt": robots, "/api/x": httpx.Response(200)})
    with pytest.raises(AccessBlocked, match="robots.txt"):
        client.get("https://example.in/api/x")
    assert [r.url.path for r in seen] == ["/robots.txt"]


def test_robots_fetched_once_per_origin():
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    client, seen = _client({"/robots.txt": robots, "/a": httpx.Response(200), "/b": httpx.Response(200)})
    client.get("https://example.in/a")
    client.get("https://example.in/b")
    assert [r.url.path for r in seen] == ["/robots.txt", "/a", "/b"]


def test_missing_robots_allows():
    client, _ = _client({"/a": httpx.Response(200)})
    assert client.get("https://example.in/a").status_code == 200


def test_robots_forbidden_means_disallow_all():
    client, _ = _client({"/robots.txt": httpx.Response(403), "/a": httpx.Response(200)})
    with pytest.raises(AccessBlocked):
        client.get("https://example.in/a")


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_block_raises_without_retry(status):
    client, seen = _client({"/a": httpx.Response(status)}, respect_robots=False)
    with pytest.raises(AccessBlocked, match=str(status)):
        client.get("https://example.in/a")
    assert len(seen) == 1


def test_other_http_errors_raise():
    client, _ = _client({"/a": httpx.Response(500)}, respect_robots=False)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://example.in/a")


def test_apis_skip_robots():
    client, seen = _client({"/v3/candles": httpx.Response(200)}, respect_robots=False)
    client.get("https://api.example.in/v3/candles")
    assert [r.url.path for r in seen] == ["/v3/candles"]
