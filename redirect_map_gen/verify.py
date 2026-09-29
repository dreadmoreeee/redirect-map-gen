"""Check a live site against the plan: one 301 hop from each old URL to its target.

When the author's redirect-checker package is importable, its trace() is used to follow
the chain; otherwise a small built-in tracer does the same (HEAD, GET on 405/501,
no auto-follow, hop counting).
"""

from __future__ import annotations

import http.client
import time
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from .net import USER_AGENT
from .rules import Plan

REDIRECT_CODES = (301, 302, 303, 307, 308)


@dataclass
class Hop:
    url: str
    status: int | None = None
    error: str | None = None


@dataclass
class Check:
    old_url: str
    url: str                 # what was requested
    expected: str            # expected target, or "410"
    hops: list[Hop] = field(default_factory=list)
    ok: bool = False
    problem: str = ""

    @property
    def redirects(self) -> int:
        return sum(1 for h in self.hops[:-1] if h.status in REDIRECT_CODES)

    def render_chain(self) -> str:
        return " -> ".join(f"{h.url} [{h.error or h.status}]" for h in self.hops)


def normalize(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.port and not ((p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)):
        host = f"{host}:{p.port}"
    path = quote(unquote(p.path or "/"), safe="/:@!$&'()*+,;=-._~")
    return urlunsplit((p.scheme.lower(), host, path, p.query, ""))


# ---------------------------------------------------------------- built-in tracer

def _request(url: str, method: str, timeout: float, user_agent: str) -> tuple[int, str | None]:
    p = urlsplit(url)
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = cls(p.hostname, p.port, timeout=timeout)
    target = (p.path or "/") + (f"?{p.query}" if p.query else "")
    try:
        conn.request(method, target, headers={"User-Agent": user_agent, "Accept": "*/*",
                                               "Connection": "close"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location")
    finally:
        conn.close()


def builtin_trace(url: str, max_hops: int = 5, timeout: float = 10.0, user_agent: str = USER_AGENT,
                  delay: float = 0.0) -> list[Hop]:
    hops: list[Hop] = []
    seen = set()
    current = url
    while True:
        if hops and delay > 0:
            time.sleep(delay)
        hop = Hop(current)
        hops.append(hop)
        try:
            status, loc = _request(current, "HEAD", timeout, user_agent)
            if status in (405, 501):
                status, loc = _request(current, "GET", timeout, user_agent)
            hop.status = status
        except (OSError, http.client.HTTPException, ValueError) as e:
            hop.error = type(e).__name__
            return hops
        if status not in REDIRECT_CODES or not loc:
            return hops
        seen.add(normalize(current))
        current = urljoin(current, loc.strip())
        if normalize(current) in seen:
            hops.append(Hop(current, error="loop"))
            return hops
        if len(hops) > max_hops:
            hops.append(Hop(current, error="too-many-hops"))
            return hops


def _checker_trace():
    try:
        from redirect_checker import trace  # type: ignore
    except ImportError:
        return None

    def run(url, max_hops, timeout, user_agent, delay):
        chain = trace(url, max_hops=max_hops, timeout=timeout, user_agent=user_agent, delay=delay)
        hops = [Hop(h.url, h.status, h.error) for h in chain.hops]
        if chain.loop and hops:
            hops[-1].error = "loop"
        elif chain.too_many_hops:
            hops.append(Hop(hops[-1].url, error="too-many-hops"))
        return hops

    return run


def pick_engine(engine: str = "auto"):
    """('redirect-checker' | 'builtin', trace function)."""
    if engine not in ("auto", "builtin", "redirect-checker"):
        raise ValueError("engine must be auto, builtin or redirect-checker")
    if engine != "builtin":
        run = _checker_trace()
        if run is not None:
            return "redirect-checker", run
        if engine == "redirect-checker":
            raise ImportError("redirect_checker is not importable")
    return "builtin", builtin_trace


# ---------------------------------------------------------------- verify

def _live_url(base: str, old: str) -> str:
    p = urlsplit(old)
    b = urlsplit(base)
    path = quote(unquote(p.path or "/"), safe="/:@!$&'()*+,;=-._~")
    return urlunsplit((b.scheme, b.netloc, path, p.query, ""))


def verify(plan: Plan, base: str, engine: str = "auto", delay: float = 1.0, timeout: float = 10.0,
           max_hops: int = 5, user_agent: str = USER_AGENT, gone: bool = True,
           relative: bool = False) -> tuple[str, list[Check]]:
    """Request each old URL on `base` and check: first answer 301, exactly one hop,
    Location equal to the expected target (relative targets resolved against `base`;
    with `relative`, every target is taken as a path on `base`), and the target itself
    answers 2xx. Gone URLs must answer 410."""
    name, trace = pick_engine(engine)
    checks: list[Check] = []
    items = [(r.source, r.target) for r in plan.redirects]
    if gone:
        items += [(g, None) for g in plan.gone]
    for i, (old, target) in enumerate(items):
        if i and delay > 0:
            time.sleep(delay)
        url = _live_url(base, old)
        if target is None:
            expected = "410"
        else:
            if relative:
                t = urlsplit(target)
                target = (t.path or "/") + (f"?{t.query}" if t.query else "")
            expected = urljoin(base, target)
        c = Check(old, url, expected)
        c.hops = trace(url, max_hops, timeout, user_agent, delay)
        first = c.hops[0]
        if target is None:
            c.ok = first.status == 410
            c.problem = "" if c.ok else f"expected 410, got {first.error or first.status}"
        elif first.error:
            c.problem = f"request failed: {first.error}"
        elif first.status != 301:
            c.problem = ("not redirected" if first.status not in REDIRECT_CODES
                         else f"{first.status} instead of 301")
        elif len(c.hops) < 2 or normalize(c.hops[1].url) != normalize(expected):
            got = c.hops[1].url if len(c.hops) > 1 else "(no Location)"
            c.problem = f"redirects to {got}, expected {expected}"
        elif c.hops[-1].error:
            c.problem = f"{c.hops[-1].error} after the first hop"
        elif c.redirects > 1:
            c.problem = f"{c.redirects} hops; expected 1"
        elif c.hops[-1].status is None or not 200 <= c.hops[-1].status < 300:
            c.problem = f"target answers {c.hops[-1].status}"
        else:
            c.ok = True
        checks.append(c)
    return name, checks
