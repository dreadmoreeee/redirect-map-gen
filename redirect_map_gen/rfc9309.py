# Vendored from robots9309 1.1.0 (MIT, Copyright (c) 2026 Marvin Palencia).
"""robots.txt matching as specified by RFC 9309 (what Google and Bing do).

urllib.robotparser applies the FIRST rule that matches, so a robots.txt that
starts with "Allow: /" and then says "Disallow: /app/" lets everything through.
RFC 9309 says the MOST SPECIFIC rule wins (the longest matching path; Allow on
a tie), with "*" and "$" wildcards, and the group of the most specific
user-agent. This module implements that, with the same small interface.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

__all__ = ["RobotsTxt", "fetch", "MAX_BYTES"]
__version__ = "1.1.0"

# RFC 9309 2.5: parsers must handle at least 500 KiB.
MAX_BYTES = 500 * 1024


def _norm(path: str) -> str:
    # Compare paths percent-encoded the same way on both sides.
    return quote(unquote(path), safe="/?=&*$%:@!,;+-._~")


class _Rule:
    __slots__ = ("allow", "pattern", "regex", "length")

    def __init__(self, allow: bool, pattern: str) -> None:
        self.allow = allow
        self.pattern = _norm(pattern)
        self.length = len(self.pattern)
        body = self.pattern
        anchored = body.endswith("$")
        if anchored:
            body = body[:-1]
        self.regex = re.compile("".join(".*" if ch == "*" else re.escape(ch) for ch in body) + ("$" if anchored else ""))

    def matches(self, path: str) -> bool:
        return bool(self.regex.match(path))


class RobotsTxt:
    """Parsed robots.txt. Use can_fetch(user_agent, url) and crawl_delay(user_agent)."""

    def __init__(self, text: str = "") -> None:
        self.groups: list[tuple[list[str], list[_Rule], float | None]] = []
        self.sitemaps: list[str] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        delay: float | None = None
        in_rules = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:  # a new group starts
                    self.groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = [], [], None, False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if agents and value:  # empty Disallow = allow everything
                    rules.append(_Rule(key == "allow", value))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    delay = float(value)
                except ValueError:
                    pass
            elif key == "sitemap" and value:
                self.sitemaps.append(value)
        if agents:
            self.groups.append((agents, rules, delay))

    def _group(self, user_agent: str) -> tuple[list[_Rule], float | None]:
        token = (user_agent or "*").split("/", 1)[0].strip().lower()
        best: list[_Rule] = []
        best_delay: float | None = None
        found = False
        # RFC 9309 2.2.1: match the product token case-insensitively; merge equal groups.
        for agents, rules, delay in self.groups:
            if any(a != "*" and a and token.startswith(a) for a in agents):
                best += rules
                best_delay = delay if delay is not None else best_delay
                found = True
        if found:
            return best, best_delay
        for agents, rules, delay in self.groups:
            if "*" in agents:
                best += rules
                best_delay = delay if delay is not None else best_delay
        return best, best_delay

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if path == "/robots.txt":
            return True
        path = _norm(path)
        rules, _ = self._group(user_agent)
        winner: _Rule | None = None
        for rule in rules:
            if rule.matches(path) and (winner is None or rule.length > winner.length
                                       or (rule.length == winner.length and rule.allow and not winner.allow)):
                winner = rule
        return True if winner is None else winner.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        return self._group(user_agent)[1]


def fetch(site: str, user_agent: str = f"robots9309/{__version__}", timeout: float = 10.0) -> "RobotsTxt":
    """Download https://host/robots.txt and apply RFC 9309 2.3.1 status rules.

    2xx: parse it. 4xx ("unavailable"): everything allowed. 5xx or a network error
    ("unreachable"): everything disallowed. Redirects are followed (up to urllib's limit).
    """
    import urllib.error
    import urllib.request

    parts = urlsplit(site if "://" in site else "https://" + site)
    url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return RobotsTxt("")
        return RobotsTxt("User-agent: *\nDisallow: /\n")
    except (urllib.error.URLError, OSError, ValueError):
        return RobotsTxt("User-agent: *\nDisallow: /\n")
    return RobotsTxt(body)
