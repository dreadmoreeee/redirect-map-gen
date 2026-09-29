"""A small, polite same-host crawler and an HTML title/link parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from .net import BudgetExceeded, Disallowed, FetchError, PoliteFetcher

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif", ".bmp",
    ".css", ".js", ".mjs", ".json", ".map", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".rar", ".7z", ".mp3", ".mp4", ".webm", ".mov", ".avi", ".wav",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".xml", ".txt",
)


class PageParser(HTMLParser):
    """Collects <title>, <a href>, <base href> and meta robots nofollow."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self.base: str | None = None
        self.nofollow = False
        self._in_title = False
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag in ("a", "area") and a.get("href"):
            if "nofollow" not in a.get("rel", "").lower().split():
                self.links.append(a["href"].strip())
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"].strip()
        elif tag == "meta" and a.get("name", "").lower() == "robots":
            values = [v.strip() for v in a.get("content", "").lower().split(",")]
            if "nofollow" in values or "none" in values:
                self.nofollow = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_page(html: str) -> PageParser:
    p = PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed HTML: keep what was parsed
        pass
    p.title = " ".join(p.title.split())
    return p


def clean_url(url: str) -> str:
    """Drop the fragment, lowercase scheme and host, empty path becomes '/'."""
    url, _ = urldefrag(url)
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def _skip_path(path: str) -> bool:
    return path.startswith("/cdn-cgi/") or path.lower().endswith(SKIP_EXTENSIONS)


@dataclass
class CrawlResult:
    urls: list[str] = field(default_factory=list)       # HTML pages that answered 200
    titles: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # url -> reason
    requests: int = 0


def crawl(start: str, max_pages: int = 200, delay: float = 1.0, fetcher: PoliteFetcher | None = None,
          timeout: float = 10.0) -> CrawlResult:
    """Breadth-first crawl of one host. Respects robots.txt (RFC 9309) and Crawl-delay,
    skips /cdn-cgi/ and non-page files, and stops after max_pages requests."""
    fetcher = fetcher or PoliteFetcher(delay=delay, timeout=timeout)
    start = clean_url(start)
    host = urlsplit(start).netloc
    result = CrawlResult()
    queue = [start]
    queued = {start}
    done: set[str] = set()
    while queue and result.requests < max_pages:
        url = queue.pop(0)
        path = urlsplit(url).path
        if _skip_path(path):
            result.skipped[url] = "cdn-cgi" if path.startswith("/cdn-cgi/") else "not a page"
            continue
        try:
            resp = fetcher.get(url)
        except Disallowed:
            result.skipped[url] = "robots.txt"
            continue
        except BudgetExceeded:
            break
        except FetchError as e:
            result.skipped[url] = f"error: {e}"
            continue
        finally:
            result.requests = fetcher.requests
        final = clean_url(resp.url)
        if urlsplit(final).netloc != host:
            result.skipped[url] = f"redirects off-site to {final}"
            continue
        if final != url and final in done:
            continue
        done.add(final)
        if resp.status != 200:
            result.skipped[url] = f"HTTP {resp.status}"
            continue
        if resp.content_type not in ("text/html", "application/xhtml+xml"):
            result.skipped[url] = f"not HTML ({resp.content_type or 'no type'})"
            continue
        page = parse_page(resp.text())
        if final not in result.titles:
            result.urls.append(final)
            result.titles[final] = page.title
        if page.nofollow:
            continue
        base = urljoin(final, page.base) if page.base else final
        for href in page.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
                continue
            link = clean_url(urljoin(base, href))
            p = urlsplit(link)
            if p.scheme not in ("http", "https") or p.netloc != host:
                continue
            if link not in queued and link not in done:
                queued.add(link)
                queue.append(link)
    return result
