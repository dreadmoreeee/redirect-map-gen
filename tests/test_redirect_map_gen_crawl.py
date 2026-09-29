from redirect_map_gen import PoliteFetcher, crawl


def _site(serve, pages, robots="User-agent: *\nAllow: /\nDisallow: /admin/\n"):
    routes = {"/robots.txt": (200, {"Content-Type": "text/plain"}, robots)}
    srv = serve(routes)
    routes.update({
        "/": pages.html("Home", ["/about.html", "/admin/login", "/cdn-cgi/l/email-protection",
                                 "/style.css", "mailto:a@b.c", "/old", "/about.html#team",
                                 "https://elsewhere.example/x", "/missing"]),
        "/about.html": pages.html("About", ["/", "/team/"]),
        "/team/": pages.html("Team", ["/team/marvin/"]),
        "/team/marvin/": pages.html("Marvin", []),
        "/old": pages.redirect("/about.html"),
        "/admin/login": pages.html("Secret", []),
    })
    return srv


def test_crawl_same_host_robots_and_cdn_cgi(serve, pages):
    srv = _site(serve, pages)
    res = crawl(srv.base + "/", delay=0)
    b = srv.base
    assert res.urls == [b + "/", b + "/about.html", b + "/team/", b + "/team/marvin/"]
    assert res.titles[b + "/about.html"] == "About"
    assert res.skipped[b + "/admin/login"] == "robots.txt"
    assert res.skipped[b + "/cdn-cgi/l/email-protection"] == "cdn-cgi"
    assert res.skipped[b + "/missing"] == "HTTP 404"
    requested = [p for _, p, _, _ in srv.log]
    assert "/admin/login" not in requested and not any(p.startswith("/cdn-cgi/") for p in requested)
    assert "/style.css" not in requested
    assert requested.count("/robots.txt") == 1
    assert all(m == "GET" for m, _, _, _ in srv.log)
    assert all(ua.startswith("redirect-map-gen/1.0") for _, _, ua, _ in srv.log)


def test_crawl_page_cap_and_delay(serve, pages):
    srv = _site(serve, pages)
    sleeps = []
    fetcher = PoliteFetcher(delay=1.0, sleep=sleeps.append)
    res = crawl(srv.base + "/", max_pages=2, fetcher=fetcher)
    assert res.requests == 2 and len(res.urls) == 2
    assert len(sleeps) == 2  # before each page (robots.txt came first)


def test_crawl_honours_crawl_delay_and_disallow_all(serve, pages):
    srv = _site(serve, pages, robots="User-agent: *\nCrawl-delay: 3\n")
    sleeps = []
    crawl(srv.base + "/", max_pages=2, fetcher=PoliteFetcher(delay=1.0, sleep=sleeps.append))
    assert sleeps and max(sleeps) > 2
    srv = _site(serve, pages, robots="User-agent: *\nAllow: /$\nDisallow: /\n")
    res = crawl(srv.base + "/", delay=0)
    assert res.urls == [srv.base + "/"]   # RFC 9309: the longer Allow: /$ wins for the home page only


def test_default_delay_is_at_least_one_second():
    assert PoliteFetcher().delay >= 1.0
    import inspect
    assert inspect.signature(crawl).parameters["delay"].default >= 1.0
