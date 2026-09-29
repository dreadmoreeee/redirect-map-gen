import os
import sys

from redirect_map_gen.cli import main


def test_end_to_end_match_rules_verify(serve, pages, tmp_path, capsys, fixtures_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "redirect_checker", None)
    out_csv = tmp_path / "map.csv"
    rc = main(["match", "--old", os.path.join(fixtures_dir, "old-urls.txt"),
               "--new", os.path.join(fixtures_dir, "new-urls.txt"), "-o", str(out_csv)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "12 old URL(s) vs 12 new" in err
    text = out_csv.read_text(encoding="utf-8")
    assert text.startswith("old_url,new_url,confidence,method,status,notes\n")

    rc = main(["rules", str(out_csv), "-f", "caddy", "--relative"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "redir /about-us.php /about-us/ permanent" in out
    assert "/careers.html" not in out.split("# Paste", 1)[1]
    assert "not applied /careers.html: below-threshold" in err

    # Serve the generated redirects locally and verify them.
    routes = {}
    for line in out.splitlines():
        if line.startswith("redir "):
            _, src, tgt, _ = line.split()
            routes[src] = pages.redirect(tgt)
            routes[tgt] = pages.html(tgt)
    srv = serve(routes)
    rc = main(["verify", str(out_csv), "--base", srv.base, "--delay", "0", "--relative"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "engine: builtin" in out and "9 checked: 9 ok, 0 failed" in out
    assert f"OK    {srv.base}/caf%C3%A9-menu.html -> {srv.base}/cafe-menu/" in out
    # Without --relative the targets are on new.example.com, which the local server is not.
    rc = main(["verify", str(out_csv), "--base", srv.base, "--delay", "0"])
    assert rc == 1 and "9 failed" in capsys.readouterr().out
    del routes["/team/"]
    rc = main(["verify", str(out_csv), "--base", srv.base, "--delay", "0", "--relative", "-v"])
    out = capsys.readouterr().out
    assert rc == 1 and "(target answers 404)" in out and "1 failed" in out


def test_verify_all_ok(serve, pages, tmp_path, capsys):
    csv_path = tmp_path / "m.csv"
    csv_path.write_text("old_url,new_url,confidence,method,status,notes\n"
                        "/a.html,/a/,0.95,slug,ok,\n/b.php,/b/,0.4,similar,approved,\n"
                        "/c.html,/c/,0.4,similar,review,\n", encoding="utf-8")
    srv = serve({"/a.html": pages.redirect("/a/"), "/a/": pages.html("A"),
                 "/b.php": pages.redirect("/b/"), "/b/": pages.html("B")})
    rc = main(["verify", str(csv_path), "--base", srv.base, "--delay", "0", "--engine", "builtin"])
    out = capsys.readouterr().out
    assert rc == 0 and "2 checked: 2 ok, 0 failed" in out


def test_crawl_and_urls_commands(serve, pages, tmp_path, capsys, fixtures_dir):
    routes = {"/robots.txt": (404, {}, b"")}
    routes["/"] = pages.html("Home", ["/x/"])
    routes["/x/"] = pages.html("X", [])
    srv = serve(routes)
    assert main(["crawl", srv.base + "/", "--delay", "0"]) == 0
    assert capsys.readouterr().out.splitlines() == [srv.base + "/", srv.base + "/x/"]
    assert main(["urls", os.path.join(fixtures_dir, "sitemap-posts.xml")]) == 0
    assert capsys.readouterr().out.splitlines() == ["https://example.com/blog/first-post/",
                                                    "https://example.com/about/"]


def test_usage_errors(capsys, tmp_path):
    assert main(["urls", str(tmp_path / "nope.txt")]) == 2
    assert "error:" in capsys.readouterr().err
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n", encoding="utf-8")
    assert main(["rules", str(bad), "-f", "nginx"]) == 2
    assert main(["match", "--new", str(bad)]) == 2


def test_match_with_titles_is_polite_and_capped(serve, pages, tmp_path, capsys):
    robots = (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
    old = serve({"/robots.txt": robots,
                 "/p.php?id=7": pages.html("Blue Widget Deluxe | Old Shop"),
                 "/q.php?id=8": pages.html("Red Gadget | Old Shop")})
    new = serve({"/robots.txt": robots,
                 "/shop/deluxe-blue/": pages.html("Blue Widget Deluxe - New Shop"),
                 "/contact/": pages.html("Contact - New Shop"),
                 "/": pages.html("Welcome - New Shop")})
    old_list = tmp_path / "old.txt"
    old_list.write_text("/p.php?id=7\n/q.php?id=8\n", encoding="utf-8")
    new_list = tmp_path / "new.txt"
    new_list.write_text("".join(new.base + p + "\n" for p in ("/", "/contact/", "/shop/deluxe-blue/")),
                        encoding="utf-8")
    rc = main(["match", "--old", str(old_list), "--new", str(new_list), "--old-base", old.base,
               "--titles", "--title-limit", "2", "--delay", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    rows = [ln.split(",") for ln in out.splitlines()[1:]]
    assert rows[0][:2] == [old.base + "/p.php?id=7", new.base + "/shop/deluxe-blue/"]
    assert rows[0][3] == "title"
    pages_new = [p for m, p, _, _ in new.log if p != "/robots.txt"]
    assert len(pages_new) <= 2                              # --title-limit per side
    assert all(m == "GET" for m, _, _, _ in old.log + new.log)
    times = sorted(t for _, _, _, t in old.log + new.log)
    assert all(b - a >= 0.95 for a, b in zip(times, times[1:]))  # >= 1 s apart even with --delay 0
    assert main(["match", "--old", str(old_list), "--new", str(new_list), "--titles"]) == 2
