"""URL lists from sitemaps (urlset, sitemap index, .gz), CSV or plain-text files."""

from __future__ import annotations

import csv
import gzip
import io
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .net import Disallowed, FetchError, PoliteFetcher

URL_COLUMNS = ("url", "loc", "address", "old_url", "from", "source", "page", "link")
MAX_SITEMAPS = 50


class SourceError(Exception):
    """A source could not be read or parsed."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(data: bytes) -> tuple[str, list[str]]:
    """Return ("urlset" | "sitemapindex", [loc, ...]). Gzip is detected by its magic bytes."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as e:
            raise SourceError(f"bad gzip data: {e}") from e
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SourceError(f"not a valid sitemap XML: {e}") from e
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SourceError(f"unexpected root element <{kind}>; expected <urlset> or <sitemapindex>")
    child = "sitemap" if kind == "sitemapindex" else "url"
    locs = []
    for el in root:
        if _local(el.tag) != child:
            continue
        for sub in el:  # only the direct <loc>, not xhtml:link alternates
            if _local(sub.tag) == "loc" and sub.text and sub.text.strip():
                locs.append(sub.text.strip())
                break
    return kind, locs


def _is_url(s: str) -> bool:
    return urlsplit(s).scheme in ("http", "https")


def load_sitemap(source: str, fetcher: PoliteFetcher | None = None,
                 max_sitemaps: int = MAX_SITEMAPS, skipped: list | None = None) -> list[str]:
    """Page URLs from a sitemap or sitemap index, local or remote, following child sitemaps.

    For a local index, each child sitemap is first looked up next to it by file name,
    so a downloaded set of sitemaps works offline. Child sitemaps that robots.txt
    disallows or that fail are skipped (and appended to `skipped` as (url, reason)).
    """
    queue = [source]
    seen: set[str] = set()
    pages: list[str] = []
    base_dir = None if _is_url(source) else os.path.dirname(os.path.abspath(source))
    while queue:
        src = queue.pop(0)
        if src in seen:
            continue
        seen.add(src)
        if len(seen) > max_sitemaps:
            raise SourceError(f"more than {max_sitemaps} sitemaps; stopping")
        try:
            data = _read(src, fetcher, base_dir)
        except (Disallowed, FetchError, SourceError) as e:
            reason = "disallowed by robots.txt" if isinstance(e, Disallowed) else str(e)
            if src == source:
                raise SourceError(f"{src}: {reason}") from e
            if skipped is not None:
                skipped.append((src, reason))
            continue
        kind, locs = parse_sitemap(data)
        if kind == "sitemapindex":
            queue.extend(locs)
        else:
            pages.extend(locs)
    return _dedupe(pages)


def _read(src: str, fetcher: PoliteFetcher | None, base_dir: str | None) -> bytes:
    if _is_url(src):
        if base_dir is not None:
            name = os.path.basename(urlsplit(src).path)
            candidate = os.path.join(base_dir, name)
            if name and os.path.isfile(candidate):
                with open(candidate, "rb") as fh:
                    return fh.read()
        fetcher = fetcher or PoliteFetcher()
        resp = fetcher.get(src)
        if resp.status != 200:
            raise SourceError(f"{src}: HTTP {resp.status}")
        return resp.body
    try:
        with open(src, "rb") as fh:
            return fh.read()
    except OSError as e:
        raise SourceError(f"cannot read {src}: {e}") from e


def parse_list(text: str) -> list[str]:
    """URLs or paths from CSV (a url-like column or the first column) or one per line."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    first = lines[0]
    if "," in first or "\t" in first:
        dialect = "excel-tab" if "\t" in first and "," not in first else "excel"
        rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect=dialect))
        col = 0
        header = [c.strip().lower() for c in rows[0]]
        if not any(_looks_like_url(c) for c in rows[0]):
            for name in URL_COLUMNS:
                if name in header:
                    col = header.index(name)
                    break
            rows = rows[1:]
        return _dedupe(r[col].strip() for r in rows if len(r) > col and r[col].strip())
    return _dedupe(ln.strip() for ln in lines)


def _looks_like_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("/") or _is_url(s)


def load_list(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return parse_list(fh.read())
    except OSError as e:
        raise SourceError(f"cannot read {path}: {e}") from e


def load_urls(spec: str, fetcher: PoliteFetcher | None = None) -> list[str]:
    """A remote sitemap URL, or a local sitemap (.xml, .gz), CSV or text file."""
    if _is_url(spec):
        return load_sitemap(spec, fetcher)
    lower = spec.lower()
    if lower.endswith((".xml", ".gz")):
        return load_sitemap(spec, fetcher)
    if not os.path.isfile(spec):
        raise SourceError(f"no such file: {spec}")
    with open(spec, "rb") as fh:
        head = fh.read(512)
    if head[:2] == b"\x1f\x8b" or head.lstrip().startswith(b"<"):
        return load_sitemap(spec, fetcher)
    return load_list(spec)


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
