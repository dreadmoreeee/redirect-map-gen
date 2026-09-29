# redirect-map-gen

Build the redirect map for a site migration: match every **old URL** to a **new URL** with a confidence score, review it as a CSV, turn it into **Caddy, nginx, Apache, Netlify or Cloudflare** rules, and verify the result on the live site. Standard library only, Python 3.10+.

```
redirect-map-gen match --old old-sitemap.xml --new https://example.com/sitemap.xml -o review.csv
# edit review.csv: approve, fix or mark rows as gone
redirect-map-gen rules review.csv --format nginx --relative
redirect-map-gen verify review.csv --base https://example.com --relative
```

Why another redirect tool:

- **It never maps a page silently.** Every pair has a confidence (0-1) and a method. Only rows at or above the threshold (default 0.7), or rows you mark `approved`, become rules. A page with no equivalent gets its parent section or the home page as a **suggestion** with low confidence, never an automatic redirect.
- **Matching that understands real URLs.** Accents (`/caf%C3%A9-menu.html` -> `/cafe-menu/`), case, `.html/.htm/.php/.asp`, `index.html`, `-` vs `_` and English/French/Spanish stop words (`/es/sobre-nosotros` -> `/es/nosotros/`) are normalized; a page moved to another directory keeps its name (`/projects/x.php` -> `/work/x/`); `/es/...` prefers `/es/...` targets.
- **One hop, no loops.** Chains A -> B -> C become A -> C; loops and self-redirects are dropped and reported.
- **Rules that do not bite back.** `/es/index.html -> /es/` loops forever on nginx and Apache if written naively (the server maps `/es/` back to `index.html` internally). The generated rules guard against that.
- **Polite.** Crawling and title fetching respect robots.txt with RFC 9309 matching (the most specific rule wins, unlike `urllib.robotparser`), wait at least 1 second between requests, stay on one host, skip `/cdn-cgi/` and stop at a page cap.

## Measured result

The "old site" is invented: a plausible list of 12 URLs from an older version of my portfolio ([examples/old-portfolio-urls.txt](examples/old-portfolio-urls.txt)), including pages that no longer exist. The new side is the **real** sitemap of [marvin.demarkstudio.ca](https://marvin.demarkstudio.ca), fetched once with a single GET after checking its robots.txt (`User-agent: * / Allow: /`) and saved as [examples/new-sitemap.xml](examples/new-sitemap.xml). Output is pasted as printed.

```
$ python -m redirect_map_gen urls examples/new-sitemap.xml
6 URL(s)
https://marvin.demarkstudio.ca/
https://marvin.demarkstudio.ca/es/
https://marvin.demarkstudio.ca/work/demark-studio/
https://marvin.demarkstudio.ca/work/triple-a-woodworks/
https://marvin.demarkstudio.ca/work/stopping-form-spam/
https://marvin.demarkstudio.ca/work/auditing-my-own-sites/

$ python -m redirect_map_gen match --old examples/old-portfolio-urls.txt --new examples/new-sitemap.xml -o examples/review.csv
12 old URL(s) vs 6 new: home 6, slug 2, slug-last 2, similar 2 | ok 4, review 8

$ cat examples/review.csv
old_url,new_url,confidence,method,status,notes
/index.html,https://marvin.demarkstudio.ca/,0.95,slug,ok,/index.html -> /
/portfolio.html,https://marvin.demarkstudio.ca/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
/about.php,https://marvin.demarkstudio.ca/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
/contact.html,https://marvin.demarkstudio.ca/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
/projects/demark-studio.php,https://marvin.demarkstudio.ca/work/demark-studio/,0.85,slug-last,ok,same page name in a different directory
/projects/triple_a_woodworks.html,https://marvin.demarkstudio.ca/work/triple-a-woodworks/,0.85,slug-last,ok,same page name in a different directory
/blog/stop-form-spam.html,https://marvin.demarkstudio.ca/work/stopping-form-spam/,0.45,similar,review,path similarity 0.50
/blog/auditing-my-sites.html,https://marvin.demarkstudio.ca/work/auditing-my-own-sites/,0.49,similar,review,path similarity 0.54
/blog/old-post.html,https://marvin.demarkstudio.ca/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
/services/wordpress-maintenance.html,https://marvin.demarkstudio.ca/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
/es/index.html,https://marvin.demarkstudio.ca/es/,0.95,slug,ok,/es/index.html -> /es/
/es/proyectos.html,https://marvin.demarkstudio.ca/es/,0.10,home,review,"no equivalent page; home suggested, but search engines treat that as a soft 404: set status=gone for a 410 if the content is gone"
```

What the matcher got right on its own, and what it did not:

- **4 confident matches (status ok):** the two index pages (0.95) and the two project pages that moved from `/projects/*.php|.html` to `/work/*/` (0.85, including `triple_a_woodworks` -> `triple-a-woodworks`).
- **2 correct but low-confidence matches (0.45 and 0.49):** `/blog/stop-form-spam.html` -> `/work/stopping-form-spam/` and `/blog/auditing-my-sites.html` -> `/work/auditing-my-own-sites/`. The words differ too much for a path-only match to be trusted, so they are left for review. They are right, but the tool does not know that.
- **6 pages with no equivalent (0.10):** the new site has no about, contact, blog or services pages, and no Spanish project list. The tool only suggests the home page (the Spanish home for `/es/proyectos.html`) and says why that is risky.

Then I reviewed the CSV by hand, as intended: approved the two blog matches, sent `/portfolio.html` and `/es/proyectos.html` to the home pages that now list the projects, marked the old blog post and the services page as `gone`, and left about/contact unresolved ([examples/review-edited.csv](examples/review-edited.csv)):

```
$ python -m redirect_map_gen rules examples/review-edited.csv --format caddy --relative --gone -o examples/Caddyfile.redirects
not applied /about.php: below-threshold: status=review confidence=0.10
not applied /contact.html: below-threshold: status=review confidence=0.10
8 redirect(s), 0 chain(s) collapsed, 2 gone, 2 below-threshold

$ cat examples/Caddyfile.redirects
# Generated by redirect-map-gen: 8 redirect(s), 0 chain(s) collapsed, 2 gone, 2 below-threshold.
# 301, one hop per URL; threshold 0.70; query string dropped.
# Paste inside your site block. Caddy path matchers are exact unless they contain *.
redir /index.html / permanent
redir /portfolio.html / permanent
redir /projects/demark-studio.php /work/demark-studio/ permanent
redir /projects/triple_a_woodworks.html /work/triple-a-woodworks/ permanent
redir /blog/stop-form-spam.html /work/stopping-form-spam/ permanent
redir /blog/auditing-my-sites.html /work/auditing-my-own-sites/ permanent
redir /es/index.html /es/ permanent
redir /es/proyectos.html /es/ permanent
respond /blog/old-post.html 410
respond /services/wordpress-maintenance.html 410

$ python -m pytest -q -p no:cacheprovider --import-mode=importlib redirect-map-gen
............................................                             [100%]
44 passed in 12.77s
```

`verify` was **not** run against marvin.demarkstudio.ca: these old URLs never existed there, so it would only report failures. It is exercised in the tests against local servers that issue correct, 302, chained, wrong-target, looping and 410 responses. Title matching was not demonstrated live either, for the same reason (the old pages cannot be fetched); it is tested locally.

## How matching works

In this order, first hit wins:

| Method | Confidence | Example |
|---|---|---|
| `exact` | 1.00 | same path and query |
| `slug` | 0.95 (0.80 if several candidates) | `/About_Us.php` -> `/about-us/`, `/caf%C3%A9-menu.html` -> `/cafe-menu/`, `/es/index.html` -> `/es/` |
| `slug-last` | 0.85 (0.75 if several) | same page name, other directory, same language: `/products/blue-widget.php` -> `/shop/widgets/blue-widget/` |
| `similar` | 0.9 x score | score = 0.6 x token Jaccard + 0.4 x difflib ratio, x 0.5 across languages (x 0.85 if one side has no prefix); accepted from 0.45 |
| `title` | up to 0.90 | only with `--titles`, only for pairs still below the threshold, at most 60 pages per side; the repeated " \| Site name" part is ignored |
| `parent` | 0.30 | no match, but the parent section exists: `/blog/old-news.html` -> `/blog/` |
| `home` | 0.10 | no match at all: home of the same language, for review |

With `--unmatched gone`, pages without a candidate become 410 candidates (`status=gone`, empty `new_url`) instead of home suggestions.

## The review CSV

Columns: `old_url,new_url,confidence,method,status,notes`. Edit it in any spreadsheet. `rules` and `verify` read the `status` column:

| status | Becomes |
|---|---|
| `ok`, `review` or empty | a 301 if `confidence` >= `--threshold` (default 0.7) |
| `approved` | a 301 whatever the confidence |
| `gone` | a 410 with `--gone` (Caddy, nginx, Apache) |
| anything else (`skip`, ...) | nothing |

Then chains are collapsed to one hop, loops and self-redirects are dropped, and duplicates keep the first row. Everything that is not applied is listed on stderr with the reason.

## Server rules

```
redirect-map-gen rules review.csv --format caddy|nginx|nginx-map|apache|netlify|cloudflare
    [--threshold 0.7] [--keep-query] [--relative] [--gone] [--host old.example.com] [-o FILE]
```

| Format | Output | Notes |
|---|---|---|
| `caddy` | `redir /old /new permanent` | `--keep-query` appends `{?query}`; non-ASCII paths are matched in `%XX` form |
| `nginx` | `location = /old { return 301 /new; }` | index files are matched on `$request_uri` so the `index` directive cannot loop; `$` in targets is percent-encoded |
| `nginx-map` | two `map` blocks and one `if` | for thousands of URLs; keys are the path as the client sends it |
| `apache` | `.htaccess` with `RewriteRule ^old\.html$ /new [R=301,L,NE,QSD]` | not `Redirect 301`, which also catches every path below and always keeps the query; regex and `$`/`%` escaped, non-ASCII as `\xHH`, query sources via `RewriteCond %{QUERY_STRING}`; `REDIRECT_STATUS` and `THE_REQUEST` guards prevent loops with clean-URL rewrites and DirectoryIndex |
| `netlify` | `_redirects`: `/old  /new  301` | Netlify keeps the query string by itself; rules are shadowed while a file still exists at the old path |
| `cloudflare` | Bulk Redirects list CSV (no header): `host/old,https://host/new,301,FALSE,FALSE,FALSE,FALSE` | needs `--host` for relative old URLs; `--keep-query` sets preserve_query_string |

`--relative` writes targets as paths (same host). Old URLs with a query string are written only where the format can match one (Apache); elsewhere they are listed as a comment.

## verify

```
redirect-map-gen verify review.csv --base https://example.com [--relative] [--gone] [--engine auto|builtin|redirect-checker] [--delay 1] [-v]
```

Each old URL is requested on `--base` without following redirects. It passes when the first answer is a **301**, the `Location` is the expected target, and the target answers 2xx: exactly **one hop**. With `--gone`, gone rows must answer 410. Exit code 1 if anything fails.

If my [redirect-checker](https://github.com/dreadmoreeee/redirect-checker) is importable, its `trace()` follows the chain; otherwise a small built-in tracer does the same (HEAD, GET when the server answers 405/501, manual hop counting). The output says which one ran (`engine: redirect-checker` or `engine: builtin`).

## Input

`--old` and `--new` each take, independently:

- a sitemap URL or file: `urlset`, sitemap index (children are followed, and looked up next to a local index first), `.xml.gz`;
- a CSV (a `url`/`loc`/`address`/`from` column, or the first column) or a text file, one URL or path per line, `#` for comments;
- or `--old-crawl URL` / `--new-crawl URL`: a breadth-first crawl of one host, robots.txt respected, `/cdn-cgi/` and non-page files skipped, `--max-pages` (default 200), `--delay` (default 1 s, raised to the site's `Crawl-delay`).

User-Agent: `redirect-map-gen/1.0 (+https://github.com/dreadmoreeee/redirect-map-gen)`. `urls` and `crawl` print a list, useful to save a sitemap or a crawl before the old site goes away.

## Library

```python
from redirect_map_gen import match, build_plan, render, verify

rows = match(["/caf%C3%A9-menu.html", "/es/sobre-nosotros.php", "/blog/2019/old.html"],
             ["https://example.com/cafe-menu/", "https://example.com/es/nosotros/", "https://example.com/blog/"])
[(r.new_url, r.confidence, r.method, r.status) for r in rows]
# [('https://example.com/cafe-menu/', 0.95, 'slug', 'ok'),
#  ('https://example.com/es/nosotros/', 0.95, 'slug', 'ok'),
#  ('https://example.com/blog/', 0.3, 'parent', 'review')]
plan = build_plan(rows, threshold=0.7)
render(plan, "nginx", relative=True)   # ... 'location = /es/sobre-nosotros.php { return 301 /es/nosotros/; }'
engine, checks = verify(plan, "https://example.com", delay=1)
```

## Tests

```
python -m pytest -q -p no:cacheprovider --import-mode=importlib
```

No internet needed: the tests start local HTTP servers on random free ports for the crawler (robots.txt, Crawl-delay, `/cdn-cgi/`, page cap), remote sitemap indexes, title fetching and `verify` (301, 302, chains, loops, wrong targets, 404 targets, 410, servers that refuse HEAD). Fixture lists cover renamed pages, moved directories, deleted pages, accents, `.php/.html` removal and language prefixes; each rule format has syntax spot checks.

## Limitations

- Matching uses URLs (and optionally titles), not page content. Renamed pages with different words end up in review, as in the result above.
- A relative old URL is assumed to be on the same host as the targets when resolving chains, unless `--host` is given.
- `verify` checks the redirect, not that the target is the right page.
- The generated rules follow each server's documentation but were not run on a live Caddy, nginx, Apache, Netlify or Cloudflare setup here; the tests check their syntax, not their behaviour.
- XML is parsed with the standard library `xml.etree`; only feed it sitemaps you would open anyway.

## Author

Marvin Palencia, founder of [DeMark Studio](https://demarkstudio.ca), Miramichi, New Brunswick, Canada. Portfolio: [marvin.demarkstudio.ca](https://marvin.demarkstudio.ca)

MIT License.
