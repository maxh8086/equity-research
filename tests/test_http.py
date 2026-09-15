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


# --------------------------------------------------------------------------- #
# Overload retries: opt-in, bounded, never for a block
# --------------------------------------------------------------------------- #


def _scripted(answers: list, *, retries: int):
    """A client whose responses (or exceptions) come from `answers` in order."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    clock = FakeClock()
    client = PoliteClient(
        user_agent=UA, min_interval_seconds=0, respect_robots=False, overload_retries=retries,
        retry_backoff_seconds=60, transport=httpx.MockTransport(handler),
        monotonic=clock.monotonic, sleep=clock.sleep,
    )  # fmt: skip
    return client, seen, clock


def test_overload_not_retried_by_default():
    client, seen, _ = _scripted([httpx.Response(503), httpx.Response(200)], retries=0)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://example.in/a")
    assert len(seen) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_overload_retried_with_growing_backoff(status):
    client, seen, clock = _scripted([httpx.Response(status), httpx.Response(status), httpx.Response(200)], retries=2)
    assert client.get("https://example.in/a").status_code == 200
    assert (len(seen), clock.sleeps) == (3, [60, 120])


def test_overload_retries_are_bounded():
    client, seen, clock = _scripted([httpx.Response(504)], retries=2)
    with pytest.raises(httpx.HTTPStatusError, match="504"):
        client.get("https://example.in/a")
    assert (len(seen), clock.sleeps) == (3, [60, 120])


def test_timeout_retried_then_raised():
    client, seen, _ = _scripted([httpx.ReadTimeout("slow")], retries=1)
    with pytest.raises(httpx.ReadTimeout):
        client.get("https://example.in/a")
    assert len(seen) == 2


def test_retry_after_longer_than_backoff_is_honoured():
    client, _, clock = _scripted([httpx.Response(503, headers={"Retry-After": "300"}), httpx.Response(200)], retries=1)
    client.get("https://example.in/a")
    assert clock.sleeps == [300]


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_block_never_retried_even_when_retries_enabled(status):
    client, seen, clock = _scripted([httpx.Response(status), httpx.Response(200)], retries=3)
    with pytest.raises(AccessBlocked):
        client.get("https://example.in/a")
    assert (len(seen), clock.sleeps) == (1, [])
