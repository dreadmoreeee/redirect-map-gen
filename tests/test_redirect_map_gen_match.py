import os

import pytest

from redirect_map_gen import PathInfo, match, path_similarity, slug
from redirect_map_gen.match import clean_titles, title_similarity
from redirect_map_gen.sources import load_list

NEW = "https://new.example.com"


def _by_old(rows):
    return {r.old_url: r for r in rows}


@pytest.fixture
def fixture_rows(fixtures_dir):
    old = load_list(os.path.join(fixtures_dir, "old-urls.txt"))
    new = load_list(os.path.join(fixtures_dir, "new-urls.txt"))
    return _by_old(match(old, new))


def test_slug_normalization():
    assert slug("/caf\u00e9-menu.html") == "/cafe-menu"
    assert slug("/caf%C3%A9-menu.html") == "/cafe-menu"
    assert slug("https://x.ca/cafe-menu/") == "/cafe-menu"
    assert slug("/Our_Team.htm") == "/team"            # stop word, case, _ and .htm
    assert slug("/index.html") == "/"
    assert slug("/es/index.php") == "/es"
    assert slug("/fr-ca/default.asp") == "/fr"
    assert slug("/el-menu-de-la-casa.php") == "/menu-casa"  # Spanish stop words
    assert slug("/the-and.html") == "/the-and"          # never strip everything


def test_language_prefix_detection():
    assert PathInfo("/es/proyectos.html").lang == "es"
    assert PathInfo("/pt_br/x").lang == "pt"
    assert PathInfo("/go/x").lang is None               # not a language code
    assert PathInfo("/proyectos.html").lang is None


def test_exact_and_slug(fixture_rows):
    r = fixture_rows["/index.html"]
    assert (r.new_url, r.method, r.status) == (NEW + "/", "slug", "ok")
    assert fixture_rows["/about-us.php"].new_url == NEW + "/about-us/"
    rows = match(["https://old.example.com/a/b?x=1"], ["https://new.example.com/a/b?x=1"])
    assert (rows[0].method, rows[0].confidence) == ("exact", 1.0)


def test_accents_and_extensions(fixture_rows):
    r = fixture_rows["/caf%C3%A9-menu.html"]
    assert (r.new_url, r.method) == (NEW + "/cafe-menu/", "slug")
    rows = match(["/caf\u00e9-menu.html"], [NEW + "/cafe-menu/"])
    assert rows[0].new_url == NEW + "/cafe-menu/" and rows[0].confidence == 0.95
    assert fixture_rows["/Our_Team.htm"].new_url == NEW + "/team/"


def test_restructured_directories(fixture_rows):
    r = fixture_rows["/products/blue-widget.php"]
    assert (r.new_url, r.method, r.confidence) == (NEW + "/shop/widgets/blue-widget/", "slug-last", 0.85)
    r = fixture_rows["/es/diseno-web.asp"]
    assert r.new_url == NEW + "/es/servicios/diseno-web/"


def test_renamed_page_is_similar_but_needs_review(fixture_rows):
    r = fixture_rows["/services/web-design.html"]
    assert r.new_url == NEW + "/services/website-design/"
    assert r.method == "similar" and r.status == "review" and 0.45 <= r.confidence < 0.7


def test_language_prefix_preference():
    rows = match(["/es/servicios-web.html"], [NEW + "/fr/servicios-web/", NEW + "/es/servicios-web-pro/"])
    assert rows[0].new_url == NEW + "/es/servicios-web-pro/"
    es = PathInfo("/es/servicios-web")
    assert path_similarity(es, PathInfo("/fr/servicios-web")) == pytest.approx(0.5)
    rows = match(["/es/sobre-nosotros.html"], [NEW + "/nosotros/", NEW + "/es/nosotros/"])
    assert rows[0].new_url == NEW + "/es/nosotros/"


def test_deleted_pages_are_never_silently_mapped(fixture_rows):
    r = fixture_rows["/blog/old-news.html"]
    assert (r.new_url, r.method, r.status, r.confidence) == (NEW + "/blog/", "parent", "review", 0.3)
    r = fixture_rows["/careers.html"]
    assert (r.method, r.status, r.confidence) == ("home", "review", 0.1)
    rows = match(["/careers.html", "/es/empleo.html"], [NEW + "/", NEW + "/es/"], unmatched="gone")
    assert [(x.status, x.new_url, x.method) for x in rows] == [("gone", "", "none")] * 2
    assert "https://new.example.com/es/" in rows[1].notes
    home = match(["/es/empleo.html"], [NEW + "/", NEW + "/es/"])[0]
    assert home.new_url == NEW + "/es/"           # same-language home


def test_ok_threshold_and_bad_argument():
    rows = match(["/services/web-design.html"], [NEW + "/services/website-design/"], threshold=0.5)
    assert rows[0].status == "ok"
    with pytest.raises(ValueError):
        match(["/a"], ["/b"], unmatched="maybe")


def test_titles_rescue_low_confidence_pairs():
    titles = {
        "https://old.example.com/p?id=7": "Blue Widget Deluxe | Old Shop",
        "https://old.example.com/x": "Home | Old Shop",
        "https://old.example.com/y": "Contact | Old Shop",
        NEW + "/shop/deluxe-blue/": "Blue Widget Deluxe - New Shop",
        NEW + "/contact/": "Contact us - New Shop",
        NEW + "/": "Welcome - New Shop",
    }
    calls = []

    def fetch(urls):
        calls.append(list(urls))
        return {u: titles[u] for u in urls if u in titles}

    rows = match(["https://old.example.com/p?id=7"], [NEW + "/shop/deluxe-blue/", NEW + "/contact/", NEW + "/"],
                 titles=fetch)
    assert rows[0].method == "title" and rows[0].new_url == NEW + "/shop/deluxe-blue/"
    assert rows[0].confidence >= 0.7
    assert len(calls) == 2  # one call per side
    # High-confidence pairs never trigger title fetching.
    calls.clear()
    match(["/about.html"], [NEW + "/about/"], titles=fetch)
    assert calls == []


def test_title_cleaning_and_similarity():
    t = clean_titles({"a": "About | Site", "b": "Team | Site", "c": "Menu | Site"})
    assert t == {"a": "About", "b": "Team", "c": "Menu"}
    assert title_similarity("Caf\u00e9 menu", "Cafe Menu") == 1.0
    assert title_similarity("Pricing", "Contact") < 0.5
