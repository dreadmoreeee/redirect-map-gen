import csv
import io
import re

import pytest

from redirect_map_gen import Match, build_plan, read_csv, render, to_csv
from redirect_map_gen.rules import RulesError, apache_regex, apache_substitution


def row(old, new, conf=0.95, status="ok", method="slug"):
    return Match(old, new, conf, method, status, "")


def plan_of(*rows, **kw):
    return build_plan(list(rows), **kw)


def test_csv_round_trip_and_human_edits():
    rows = [row("/a.html", "https://n.ca/a/"), row("/b", "https://n.ca/b/", 0.4, "review")]
    text = to_csv(rows)
    assert text.splitlines()[0] == "old_url,new_url,confidence,method,status,notes"
    edited = text.replace("0.40,slug,review", "0.40,slug,approved")
    back = read_csv(edited)
    assert [(r.old_url, r.status) for r in back] == [("/a.html", "ok"), ("/b", "approved")]
    assert len(build_plan(back).redirects) == 2
    with pytest.raises(RulesError):
        read_csv("a,b\n1,2\n")
    with pytest.raises(RulesError):
        read_csv("old_url,new_url,confidence\n/a,/b,high\n")


def test_threshold_decides():
    rows = [row("/a", "/x", 0.95), row("/b", "/y", 0.6, "review"), row("/c", "/z", 0.2, "approved"),
            row("/d", "/w", 0.99, "skip"), row("/e", "", 0.0, "gone")]
    p = build_plan(rows, threshold=0.7)
    assert [r.source for r in p.redirects] == ["/a", "/c"]
    assert p.gone == ["/e"]
    assert {o for o, _ in p.dropped} == {"/b", "/d"}
    assert [r.source for r in build_plan(rows, threshold=0.5).redirects] == ["/a", "/b", "/c"]


def test_chain_is_collapsed_to_one_hop():
    p = plan_of(row("/a", "/b"), row("/b", "/c"), row("/c", "https://n.ca/d"))
    assert [(r.source, r.target) for r in p.redirects] == [
        ("/a", "https://n.ca/d"), ("/b", "https://n.ca/d"), ("/c", "https://n.ca/d")]
    assert p.redirects[0].via == ["/b", "/c"]


def test_chain_across_absolute_and_relative_urls():
    p = plan_of(row("https://old.ca/a", "https://old.ca/b"), row("/b", "/c"), host="old.ca")
    assert p.redirects[0].target == "/c"
    # a different host is not the same URL
    p = plan_of(row("https://old.ca/a", "https://other.ca/b"), row("https://old.ca/b", "/c"))
    assert p.redirects[0].target == "https://other.ca/b"


def test_loops_and_self_redirects_are_dropped():
    p = plan_of(row("/a", "/b"), row("/b", "/a"), row("/s", "/s"), row("/x", "/a"),
                row("/t/", "https://n.ca/t/"), row("/ok", "/b2"))
    assert [(r.source, r.target) for r in p.redirects] == [("/ok", "/b2")]
    reasons = dict(p.dropped)
    assert reasons["/a"].startswith("loop: /a -> /b -> /a")
    assert reasons["/x"].startswith("loop")
    assert reasons["/s"].startswith("self-redirect")
    assert reasons["/t/"].startswith("self-redirect")
    # a row pointing at a page that redirects to itself is not a loop
    p = plan_of(row("/a", "/b"), row("/b", "/b"))
    assert [(r.source, r.target) for r in p.redirects] == [("/a", "/b")]


def test_duplicates_and_gone_target_warning():
    p = plan_of(row("/a", "/x"), row("/a", "/y"), row("/b", "/g"), Match("/g", "", 0, "none", "gone"))
    assert [(r.source, r.target) for r in p.redirects] == [("/a", "/x"), ("/b", "/g")]
    assert any("duplicate" in why for _, why in p.dropped)
    assert p.warnings and "/g" in p.warnings[0]


SPECIAL = [row("/caf\u00e9 menu.html", "https://n.ca/cafe-menu/"), row("/a.php", "https://n.ca/b/"),
           row("/price$.html", "https://n.ca/p/$5%off"), row("/list.php?id=7", "https://n.ca/list/7/"),
           row("/", "https://n.ca/home/"), Match("/old-news.html", "", 0.0, "none", "gone")]


def _body(text):
    return [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]


def test_caddy():
    out = render(build_plan(SPECIAL), "caddy", relative=True, gone=True)
    body = _body(out)
    assert "redir /caf%C3%A9%20menu.html /cafe-menu/ permanent" in body
    assert "redir /a.php /b/ permanent" in body
    assert "redir /price$.html /p/$5%25off permanent" in body  # $ is literal in Caddy
    assert "redir / /home/ permanent" in body
    assert "respond /old-news.html 410" in body
    assert "# skipped /list.php?id=7" in out
    assert all(re.fullmatch(r"(redir \S+ \S+ permanent|respond \S+ 410)", ln) for ln in body)
    kq = render(build_plan(SPECIAL[1:2]), "caddy", keep_query=True)
    assert "redir /a.php https://n.ca/b/{?query} permanent" in kq


def test_nginx_location_and_map():
    out = render(build_plan(SPECIAL), "nginx", gone=True)
    body = _body(out)
    assert 'location = "/caf\u00e9 menu.html" { return 301 https://n.ca/cafe-menu/; }' in body
    assert "location = /a.php { return 301 https://n.ca/b/; }" in body
    assert 'location = "/price$.html" { return 301 https://n.ca/p/%245%25off; }' in body
    assert "location = /old-news.html { return 410; }" in body
    for ln in body:
        assert ln.count("{") == ln.count("}") and ln.endswith("}")
    kq = render(build_plan(SPECIAL[1:2]), "nginx", keep_query=True, relative=True)
    assert 'location = /a.php { return 301 "/b/$is_args$args"; }' in kq
    m = render(build_plan(SPECIAL), "nginx-map", gone=True)
    assert "map $redirect_map_gen_path $redirect_map_gen_target {" in m
    assert "    /a.php https://n.ca/b/;" in m
    assert "if ($redirect_map_gen_target) { return 301 $redirect_map_gen_target; }" in m
    assert "if ($redirect_map_gen_gone) { return 410; }" in m
    assert m.count("{") == m.count("}")


def test_apache_escaping():
    assert apache_regex("caf\u00e9 menu.html") == r"caf\xC3\xA9\x20menu\.html"
    assert apache_regex("a(1)+[b]") == r"a\(1\)\+\[b\]"
    assert apache_substitution("/p/$5%25off") == r"/p/\$5\%25off"
    out = render(build_plan(SPECIAL), "apache", gone=True)
    body = _body(out)
    assert body[0] == "<IfModule mod_rewrite.c>" and body[1] == "RewriteEngine On" and body[-1] == "</IfModule>"
    assert r"RewriteRule ^a\.php$ https://n.ca/b/ [R=301,L,NE,QSD]" in body
    assert r"RewriteRule ^caf\xC3\xA9\x20menu\.html$ https://n.ca/cafe-menu/ [R=301,L,NE,QSD]" in body
    assert r"RewriteRule ^price\$\.html$ https://n.ca/p/\$5\%25off [R=301,L,NE,QSD]" in body
    i = body.index(r"RewriteCond %{QUERY_STRING} ^id=7$")
    assert body[i + 1] == r"RewriteRule ^list\.php$ https://n.ca/list/7/ [R=301,L,NE,QSD]"
    assert "RewriteRule ^$ https://n.ca/home/ [R=301,L,NE,QSD]" in body
    assert r"RewriteRule ^old-news\.html$ - [G,L]" in body
    for ln in body:
        if ln.startswith("RewriteRule"):
            assert len(ln.split(" ")) == 4  # pattern, substitution and flags have no spaces
    kq = render(build_plan(SPECIAL[1:2]), "apache", keep_query=True)
    assert "[R=301,L,NE]" in kq and "QSD" not in kq


def test_netlify():
    out = render(build_plan(SPECIAL), "netlify", relative=True, gone=True)
    body = _body(out)
    assert "/caf%C3%A9%20menu.html  /cafe-menu/  301" in body
    assert "/a.php  /b/  301" in body
    assert all(len(ln.split()) == 3 and ln.endswith("301") for ln in body)
    assert "skipped /list.php?id=7" in out


def test_cloudflare_bulk_csv():
    out = render(build_plan(SPECIAL), "cloudflare", host="old.ca", keep_query=True)
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["old.ca/caf%C3%A9%20menu.html", "https://n.ca/cafe-menu/", "301", "TRUE", "FALSE",
                       "FALSE", "FALSE"]
    assert all(len(r) == 7 and r[2] == "301" for r in rows)
    assert not any("?" in r[0] for r in rows)       # no query strings in sources
    assert not any(r[0].startswith("http") for r in rows)
    with pytest.raises(RulesError):
        render(build_plan(SPECIAL), "cloudflare")    # relative sources need --host
    rel = render(build_plan([row("https://old.ca/a", "/b")]), "cloudflare")
    assert rel.strip() == "old.ca/a,https://old.ca/b,301,FALSE,FALSE,FALSE,FALSE"


def test_unknown_format():
    with pytest.raises(RulesError):
        render(build_plan([]), "iis")


def test_index_files_do_not_loop():
    p = build_plan([row("/es/index.html", "https://n.ca/es/"), row("/about.html", "https://n.ca/about/")])
    ng = _body(render(p, "nginx", relative=True))
    assert ng[0] == r'location = /es/index.html { if ($request_uri ~ "^/es/index\.html(?:\?|$)") { return 301 /es/; } }'
    assert ng[1] == "location = /about.html { return 301 /about/; }"
    ap = _body(render(p, "apache", relative=True))
    assert ap[2:5] == ["RewriteCond %{ENV:REDIRECT_STATUS} ^$",
                       r"RewriteCond %{THE_REQUEST} ^\S+\s/es/index\.html[?\s]",
                       r"RewriteRule ^es/index\.html$ /es/ [R=301,L,NE,QSD]"]
    assert ap[5:7] == ["RewriteCond %{ENV:REDIRECT_STATUS} ^$", r"RewriteRule ^about\.html$ /about/ [R=301,L,NE,QSD]"]
    m = render(build_plan(SPECIAL), "nginx-map")
    assert "map $request_uri $redirect_map_gen_path {" in m      # raw client path, not $uri
    assert "    /caf%C3%A9%20menu.html https://n.ca/cafe-menu/;" in m
