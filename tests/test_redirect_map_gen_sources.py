import gzip
import os
import shutil

import pytest

from redirect_map_gen import PoliteFetcher, load_sitemap, load_urls, parse_list, parse_sitemap
from redirect_map_gen.sources import SourceError


def test_sitemap_index_with_gz_child(fixtures_dir, tmp_path):
    for name in ("sitemap-index.xml", "sitemap-pages.xml"):
        shutil.copy(os.path.join(fixtures_dir, name), tmp_path / name)
    with open(os.path.join(fixtures_dir, "sitemap-posts.xml"), "rb") as fh:
        (tmp_path / "sitemap-posts.xml.gz").write_bytes(gzip.compress(fh.read()))
    urls = load_sitemap(str(tmp_path / "sitemap-index.xml"))
    # alternates ignored, whitespace trimmed, duplicates removed, order kept
    assert urls == ["https://example.com/", "https://example.com/about/", "https://example.com/es/",
                    "https://example.com/blog/first-post/"]
    assert load_urls(str(tmp_path / "sitemap-posts.xml.gz")) == [
        "https://example.com/blog/first-post/", "https://example.com/about/"]


def test_parse_sitemap_errors():
    with pytest.raises(SourceError):
        parse_sitemap(b"<html></html>")
    with pytest.raises(SourceError):
        parse_sitemap(b"not xml")
    assert parse_sitemap(b"<urlset/>") == ("urlset", [])


def test_lists_text_and_csv(fixtures_dir):
    assert parse_list("# c\n/a\n\n/b\n/a\n") == ["/a", "/b"]
    urls = load_urls(os.path.join(fixtures_dir, "old-list.csv"))
    assert urls == ["https://old.example.com/", "https://old.example.com/about.html",
                    "https://old.example.com/a,b.html"]
    assert parse_list("/x,1\n/y,2\n") == ["/x", "/y"]                    # no header
    assert parse_list("from,to\n/old,/new\n") == ["/old"]
    with pytest.raises(SourceError):
        load_urls(os.path.join(fixtures_dir, "missing.txt"))


def test_remote_sitemap_index_respects_robots(serve):
    child = gzip.compress(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                          b"<url><loc>https://example.com/one/</loc></url></urlset>")
    routes = {}
    srv = serve(routes)
    routes.update({
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\nDisallow: /private/\n"),
        "/sitemap.xml": (200, {"Content-Type": "application/xml"},
                         f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         f"<sitemap><loc>{srv.base}/s1.xml.gz</loc></sitemap>"
                         f"<sitemap><loc>{srv.base}/private/s2.xml</loc></sitemap></sitemapindex>"),
        "/s1.xml.gz": (200, {"Content-Type": "application/gzip"}, child),
    })
    skipped = []
    urls = load_sitemap(srv.base + "/sitemap.xml", PoliteFetcher(delay=0), skipped=skipped)
    assert urls == ["https://example.com/one/"]
    assert skipped == [(srv.base + "/private/s2.xml", "disallowed by robots.txt")]
    paths = [p for _, p, _, _ in srv.log]
    assert paths == ["/robots.txt", "/sitemap.xml", "/s1.xml.gz"]  # /private/ never requested
    assert all(ua.startswith("redirect-map-gen/1.0 (+https://github.com/dreadmoreeee/redirect-map-gen)")
               for _, _, ua, _ in srv.log)


def test_remote_sitemap_http_error(serve):
    srv = serve({"/robots.txt": (404, {}, b"")})
    with pytest.raises(SourceError):
        load_urls(srv.base + "/sitemap.xml", PoliteFetcher(delay=0))


def test_polite_fetcher_waits_between_requests(serve):
    srv = serve({"/robots.txt": (404, {}, b""), "/a": (200, {}, b"a"), "/b": (200, {}, b"b")})
    sleeps = []
    f = PoliteFetcher(delay=1.0, sleep=sleeps.append)
    f.get(srv.base + "/a")
    f.get(srv.base + "/b")
    assert len(sleeps) == 2 and all(0 < s <= 1.0 for s in sleeps)
    assert f.requests == 2
