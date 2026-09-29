"""Match old URLs to new URLs, each pair with a confidence (0-1) and a method label."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from .normalize import STOP_WORDS, PathInfo, jaccard, path_similarity, strip_accents

OK_THRESHOLD = 0.7
MIN_SIMILARITY = 0.45
TITLE_LIMIT = 60

CONFIDENCE = {"exact": 1.0, "slug": 0.95, "slug-last": 0.85, "parent": 0.3, "home": 0.1}


@dataclass
class Match:
    old_url: str
    new_url: str
    confidence: float
    method: str  # exact, slug, slug-last, similar, title, parent, home, none
    status: str  # ok, review, gone
    notes: str = ""


TitleFetcher = Callable[[list[str]], dict]


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _short(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


class Matcher:
    def __init__(self, new_urls: Iterable[str]) -> None:
        self.new = [PathInfo(u) for u in _dedupe(new_urls)]
        self.by_path: dict[tuple, PathInfo] = {}
        self.by_slug: dict[str, list[PathInfo]] = {}
        self.by_last: dict[tuple, list[PathInfo]] = {}
        for n in self.new:
            self.by_path.setdefault((n.path, n.query), n)
            self.by_slug.setdefault(n.slug, []).append(n)
            if n.last:
                self.by_last.setdefault((n.lang, n.last), []).append(n)

    def home(self, lang: str | None) -> str | None:
        for key in ([f"/{lang}"] if lang else []) + ["/"]:
            if key in self.by_slug:
                return self.by_slug[key][0].url
        if self.new:
            p = urlsplit(self.new[0].url)
            return f"{p.scheme}://{p.netloc}/" if p.netloc else "/"
        return None

    def ranked(self, o: PathInfo) -> list[tuple[float, PathInfo]]:
        scored = [(path_similarity(o, n), n) for n in self.new]
        scored.sort(key=lambda t: -t[0])
        return scored

    def match_one(self, url: str, min_similarity: float = MIN_SIMILARITY) -> Match | None:
        o = PathInfo(url)
        n = self.by_path.get((o.path, o.query))
        if n is not None:
            return Match(url, n.url, CONFIDENCE["exact"], "exact", "ok", "same path")
        if not o.query:
            cands = self.by_slug.get(o.slug, [])
            if cands:
                note = f"{_short(url)} -> {_short(cands[0].url)}"
                if len(cands) > 1:
                    return Match(url, cands[0].url, 0.8, "slug", "", f"{len(cands)} candidates with the same slug")
                return Match(url, cands[0].url, CONFIDENCE["slug"], "slug", "", note)
            if o.last:
                cands = self.by_last.get((o.lang, o.last), [])
                if len(cands) == 1:
                    return Match(url, cands[0].url, CONFIDENCE["slug-last"], "slug-last", "",
                                 "same page name in a different directory")
                if len(cands) > 1:
                    best = max(cands, key=lambda c: path_similarity(o, c))
                    return Match(url, best.url, 0.75, "slug-last", "",
                                 f"{len(cands)} pages with this name; picked the closest path")
        ranked = self.ranked(o)
        if ranked and ranked[0][0] >= min_similarity:
            score, n = ranked[0]
            note = f"path similarity {score:.2f}"
            if len(ranked) > 1 and ranked[1][0] >= score - 0.05 and ranked[1][0] > 0:
                note += f"; runner-up {_short(ranked[1][1].url)} ({ranked[1][0]:.2f})"
            return Match(url, n.url, round(score * 0.9, 2), "similar", "", note)
        return None

    def fallback(self, url: str, unmatched: str) -> Match:
        o = PathInfo(url)
        for i in range(len(o.rest) - 1, 0, -1):
            key = "/" + "/".join(([o.lang] if o.lang else []) + o.rest[:i])
            if key in self.by_slug:
                return Match(url, self.by_slug[key][0].url, CONFIDENCE["parent"], "parent", "review",
                             "no equivalent page; parent section suggested")
        home = self.home(o.lang) or ""
        if unmatched == "review":
            return Match(url, home, CONFIDENCE["home"], "home", "review",
                         "no equivalent page; home suggested, but search engines treat that as a "
                         "soft 404: set status=gone for a 410 if the content is gone")
        return Match(url, "", 0.0, "none", "gone",
                     f"no equivalent page; 410 candidate (to redirect instead set new_url, e.g. {home}, "
                     "and status=approved)")


def match(old_urls: Iterable[str], new_urls: Iterable[str], *, threshold: float = OK_THRESHOLD,
          min_similarity: float = MIN_SIMILARITY, unmatched: str = "review",
          titles: TitleFetcher | None = None, title_limit: int = TITLE_LIMIT) -> list[Match]:
    """Match every old URL. Order: exact path, normalized slug, same page name elsewhere,
    path similarity, then (optionally) page titles for the pairs still below the threshold.
    Old URLs without a candidate get a parent-section suggestion (review), else a
    home suggestion (unmatched="review", the default) or become 410 candidates
    (unmatched="gone"). Nothing below the threshold is applied without a human.
    """
    if unmatched not in ("gone", "review"):
        raise ValueError("unmatched must be 'gone' or 'review'")
    matcher = Matcher(new_urls)
    olds = _dedupe(old_urls)
    results: dict[str, Match | None] = {u: matcher.match_one(u, min_similarity) for u in olds}
    if titles is not None:
        low = [u for u in olds if results[u] is None or results[u].confidence < threshold]
        if low:
            _title_pass(matcher, low[:title_limit], results, titles, title_limit, threshold)
    out = []
    for u in olds:
        m = results[u] or matcher.fallback(u, unmatched)
        if not m.status:
            m.status = "ok" if m.confidence >= threshold else "review"
        out.append(m)
    return out


# ---------------------------------------------------------------- titles

_TITLE_SEP = re.compile(r"\s+[|\-:\u2013\u2014\u00b7\u2022]\s+")


def _title_parts(title: str) -> list[str]:
    return [p.strip() for p in _TITLE_SEP.split(title or "") if p.strip()]


def clean_titles(titles: dict) -> dict:
    """Drop parts repeated across most titles (the ' | Site Name' suffix)."""
    parts = {u: _title_parts(t) for u, t in titles.items() if t}
    counts = Counter(p.lower() for ps in parts.values() for p in set(ps))
    n = len(parts)
    common = {p for p, c in counts.items() if n >= 3 and c >= max(2, n / 2)}
    out = {}
    for u, ps in parts.items():
        kept = [p for p in ps if p.lower() not in common]
        out[u] = " ".join(kept or ps)
    return out


def _title_words(t: str) -> list[str]:
    toks = re.findall(r"\w+", strip_accents(t).lower())
    return [w for w in toks if w not in STOP_WORDS] or toks


def title_similarity(a: str, b: str) -> float:
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    r = SequenceMatcher(None, " ".join(wa), " ".join(wb), autojunk=False).ratio()
    return 0.5 * jaccard(wa, wb) + 0.5 * r


def _title_pass(matcher: Matcher, low: list[str], results: dict, fetch: TitleFetcher,
                limit: int, threshold: float) -> None:
    # New pages to fetch: the best path candidates of each low-confidence old URL.
    wanted: list[str] = []
    for u in low:
        for _, n in matcher.ranked(PathInfo(u))[:5]:
            if n.url not in wanted:
                wanted.append(n.url)
    for n in matcher.new:
        if len(wanted) >= limit:
            break
        if n.url not in wanted:
            wanted.append(n.url)
    old_raw = fetch(low[:limit]) or {}
    new_raw = fetch(wanted[:limit]) or {}
    old_titles, new_titles = clean_titles(old_raw), clean_titles(new_raw)
    if not old_titles or not new_titles:
        return

    def first(t: str) -> str:
        parts = _title_parts(t)
        return parts[0] if parts else ""

    for u in low:
        ot = old_titles.get(u)
        if not ot:
            continue
        o = PathInfo(u)
        best = None
        for nu, nt in new_titles.items():
            # The page-specific part usually comes first ("Pricing | Site"), so compare
            # both the cleaned titles and the first parts.
            ts = max(title_similarity(ot, nt), title_similarity(first(old_raw[u]), first(new_raw[nu])))
            ps = path_similarity(o, PathInfo(nu))
            score = 0.7 * ts + 0.3 * ps
            if ts >= 0.6 and (best is None or score > best[0]):
                best = (score, nu, ts)
        if best is None:
            continue
        conf = round(min(0.9, best[0] + 0.15), 2)
        current = results.get(u)
        if current is None or conf > current.confidence:
            results[u] = Match(u, best[1], conf, "title", "",
                               f"title similarity {best[2]:.2f}: {ot!r} ~ {new_titles[best[1]]!r}")


def fetch_titles_with(fetcher, base: str | None = None, limit: int = TITLE_LIMIT) -> TitleFetcher:
    """A TitleFetcher that GETs pages politely through a PoliteFetcher, at most `limit`
    pages per call (the matcher calls it once per side)."""
    from .crawl import parse_page
    from .net import BudgetExceeded, Disallowed, FetchError

    def run(urls: list[str]) -> dict:
        out = {}
        for u in urls[:limit]:
            full = urljoin(base, u) if base else u
            if urlsplit(full).scheme not in ("http", "https"):
                continue
            try:
                resp = fetcher.get(full)
            except (Disallowed, FetchError):
                continue
            except BudgetExceeded:
                break
            if resp.status == 200 and "html" in resp.content_type:
                title = parse_page(resp.text()).title
                if title:
                    out[u] = title
        return out

    return run
