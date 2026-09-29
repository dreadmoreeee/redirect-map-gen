import os
import sys

import pytest

from redirect_map_gen import Match, build_plan, pick_engine, verify

CHECKER_DIR = os.path.join(os.path.expanduser("~"), "redirect-checker")


def _routes(pages):
    return {
        "/old-a": pages.redirect("/new-a"),
        "/new-a": pages.html("A"),
        "/old-a2": pages.redirect("/new-a"),
        "/old-b": pages.redirect("/new-b", 302),
        "/new-b": pages.html("B"),
        "/old-c": pages.redirect("/mid"),
        "/mid": pages.redirect("/new-c"),
        "/new-c": pages.html("C"),
        "/old-d": pages.redirect("/wrong"),
        "/old-e": pages.html("E"),
        "/old-f": pages.redirect("/new-f"),   # target is a 404
        "/loop1": pages.redirect("/loop2"),
        "/loop2": pages.redirect("/loop1"),
        "/gone": (410, {}, b""),
    }


def _plan(base):
    rows = [Match(o, t, 0.9, "slug", "ok") for o, t in [
        ("/old-a", "/new-a"), ("/old-b", "/new-b"), ("/old-c", "/new-c"), ("/old-d", "/new-d"),
        ("/old-e", "/new-e"), ("/old-f", "/new-f"), ("/loop1", "/somewhere"),
        ("https://old.example.com/old-a2", base + "/new-a")]]
    rows.append(Match("/gone", "", 0.0, "none", "gone"))
    return build_plan(rows)


def _problems(checks):
    return {c.old_url: (c.ok, c.problem) for c in checks}


def _assert_results(checks):
    p = _problems(checks)
    assert p["/old-a"] == (True, "")
    assert p["https://old.example.com/old-a2"] == (True, "")  # host of the old URL replaced by --base
    assert p["/old-b"] == (False, "302 instead of 301")
    assert p["/old-c"][0] is False and "expected" in p["/old-c"][1]  # first hop goes to /mid
    assert p["/old-d"][0] is False and "/wrong" in p["/old-d"][1]
    assert p["/old-e"] == (False, "not redirected")
    assert p["/old-f"] == (False, "target answers 404")
    assert p["/loop1"][0] is False
    assert p["/gone"] == (True, "")


def test_verify_builtin(serve, pages):
    srv = serve(_routes(pages))
    name, checks = verify(_plan(srv.base), srv.base, engine="builtin", delay=0)
    assert name == "builtin"
    _assert_results(checks)
    assert {m for m, _, _, _ in srv.log} == {"HEAD"}   # no GET needed, no auto-follow
    c = next(c for c in checks if c.old_url == "/old-a")
    assert [h.status for h in c.hops] == [301, 200] and c.redirects == 1


def test_verify_chain_counts_hops(serve, pages):
    routes = _routes(pages)
    srv = serve(routes)
    routes["/old-c"] = pages.redirect("/new-c")
    routes["/new-c"] = pages.redirect("/new-c/")
    routes["/new-c/"] = pages.html("C")
    _, checks = verify(build_plan([Match("/old-c", "/new-c", 1, "exact", "ok")]), srv.base,
                       engine="builtin", delay=0)
    assert checks[0].problem == "2 hops; expected 1"


def test_verify_get_fallback_when_head_is_refused(serve, pages):
    srv = serve(_routes(pages), no_head=True)
    _, checks = verify(_plan(srv.base), srv.base, engine="builtin", delay=0)
    _assert_results(checks)
    assert "GET" in {m for m, _, _, _ in srv.log}


def test_auto_engine_falls_back_to_builtin(monkeypatch):
    monkeypatch.setitem(sys.modules, "redirect_checker", None)  # makes the import fail
    assert pick_engine("auto")[0] == "builtin"
    with pytest.raises(ImportError):
        pick_engine("redirect-checker")
    with pytest.raises(ValueError):
        pick_engine("curl")


@pytest.mark.skipif(not os.path.isdir(os.path.join(CHECKER_DIR, "redirect_checker")),
                    reason="redirect-checker source not available")
def test_verify_with_redirect_checker(serve, pages, monkeypatch):
    monkeypatch.syspath_prepend(CHECKER_DIR)
    for mod in [m for m in sys.modules if m.startswith("redirect_checker")]:
        monkeypatch.delitem(sys.modules, mod)
    try:
        srv = serve(_routes(pages))
        name, checks = verify(_plan(srv.base), srv.base, engine="auto", delay=0)
        assert name == "redirect-checker"
        _assert_results(checks)
    finally:
        for mod in [m for m in sys.modules if m.startswith("redirect_checker")]:
            del sys.modules[mod]
