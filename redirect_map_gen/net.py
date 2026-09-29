"""Polite HTTP: one User-Agent, robots.txt (RFC 9309) per host, a delay between requests."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import rfc9309

USER_AGENT = "redirect-map-gen/1.0 (+https://github.com/dreadmoreeee/redirect-map-gen)"
MAX_BODY = 10 * 1024 * 1024


class FetchError(Exception):
    """Network failure (DNS, refused, timeout, TLS)."""


class Disallowed(Exception):
    """robots.txt does not allow this URL for our User-Agent."""


class BudgetExceeded(Exception):
    """The request budget of the fetcher is used up."""


@dataclass
class Response:
    url: str  # final URL after urllib followed redirects
    status: int
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    def text(self) -> str:
        charset = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


def http_get(url: str, timeout: float = 10.0, user_agent: str = USER_AGENT,
             max_bytes: int = MAX_BODY) -> Response:
    """Plain GET (urllib follows redirects). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            return Response(resp.geturl(), resp.status,
                            {k.lower(): v for k, v in resp.headers.items()}, body)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(64 * 1024)
        except OSError:
            pass
        return Response(e.geturl() or url, e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"{url}: {getattr(e, 'reason', e)}") from e


class PoliteFetcher:
    """GET with robots.txt checks, a minimum delay between requests and an optional budget.

    The delay is max(delay, Crawl-delay of the host). robots.txt itself is fetched once
    per host and counts as a request for the delay (not for the budget).
    """

    def __init__(self, user_agent: str = USER_AGENT, delay: float = 1.0, timeout: float = 10.0,
                 max_requests: int | None = None, sleep=time.sleep, clock=time.monotonic) -> None:
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests = 0
        self._robots: dict[str, rfc9309.RobotsTxt] = {}
        self._last: float | None = None
        self._sleep = sleep
        self._clock = clock

    def _wait(self, host_delay: float | None = None) -> None:
        delay = max(self.delay, host_delay or 0.0)
        if self._last is not None and delay > 0:
            remaining = self._last + delay - self._clock()
            if remaining > 0:
                self._sleep(remaining)

    def robots(self, url: str) -> rfc9309.RobotsTxt:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}".lower()
        if origin not in self._robots:
            self._wait()
            self._robots[origin] = rfc9309.fetch(origin, user_agent=self.user_agent, timeout=self.timeout)
            self._last = self._clock()
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return self.robots(url).can_fetch(self.user_agent, url)

    def get(self, url: str) -> Response:
        rules = self.robots(url)
        if not rules.can_fetch(self.user_agent, url):
            raise Disallowed(url)
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BudgetExceeded(url)
        self._wait(rules.crawl_delay(self.user_agent))
        self.requests += 1
        try:
            return http_get(url, self.timeout, self.user_agent)
        finally:
            self._last = self._clock()
