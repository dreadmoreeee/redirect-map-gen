"""Command line: urls, crawl, match, rules, verify."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from urllib.parse import urljoin

from . import __version__
from .crawl import crawl
from .match import OK_THRESHOLD, TITLE_LIMIT, fetch_titles_with, match
from .net import PoliteFetcher
from .rules import FORMATS, RulesError, build_plan, load_csv, render, to_csv
from .sources import SourceError, load_urls
from .verify import pick_engine, verify


def _out(text: str, path: str | None) -> None:
    if path:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _positive_delay(s: str) -> float:
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError("delay must be >= 0")
    return v


def _side(args, which: str) -> list[str]:
    spec = getattr(args, which)
    start = getattr(args, f"{which}_crawl")
    if bool(spec) == bool(start):
        raise SourceError(f"give exactly one of --{which} or --{which}-crawl")
    if start:
        res = crawl(start, max_pages=args.max_pages, delay=args.delay, timeout=args.timeout)
        _err(f"crawled {start}: {len(res.urls)} page(s) in {res.requests} request(s), "
             f"{len(res.skipped)} skipped")
        return res.urls
    return load_urls(spec, PoliteFetcher(delay=args.delay, timeout=args.timeout))


def cmd_urls(args) -> int:
    urls = load_urls(args.source, PoliteFetcher(delay=args.delay, timeout=args.timeout))
    _out("".join(u + "\n" for u in urls), args.output)
    _err(f"{len(urls)} URL(s)")
    return 0


def cmd_crawl(args) -> int:
    res = crawl(args.start, max_pages=args.max_pages, delay=args.delay, timeout=args.timeout)
    _out("".join(u + "\n" for u in res.urls), args.output)
    for u, why in res.skipped.items():
        _err(f"skipped {u}: {why}")
    _err(f"{len(res.urls)} page(s), {res.requests} request(s)")
    return 0


def cmd_match(args) -> int:
    old = _side(args, "old")
    new = _side(args, "new")
    if args.old_base:
        old = [urljoin(args.old_base, u) for u in old]
    if not old or not new:
        raise SourceError("an URL list is empty")
    titles = None
    if args.titles:
        if not args.old_base and any(not u.startswith(("http://", "https://")) for u in old):
            raise SourceError("--titles needs --old-base when the old list has relative paths")
        fetcher = PoliteFetcher(delay=max(args.delay, 1.0), timeout=args.timeout)
        titles = fetch_titles_with(fetcher, limit=args.title_limit)
    rows = match(old, new, threshold=args.threshold, unmatched=args.unmatched, titles=titles,
                 title_limit=args.title_limit)
    _out(to_csv(rows), args.output)
    methods = Counter(r.method for r in rows)
    statuses = Counter(r.status for r in rows)
    _err(f"{len(rows)} old URL(s) vs {len(new)} new: "
         + ", ".join(f"{k} {v}" for k, v in methods.most_common())
         + " | " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))
    return 0


def cmd_rules(args) -> int:
    rows = load_csv(args.csv)
    plan = build_plan(rows, threshold=args.threshold, host=args.host)
    text = render(plan, args.format, keep_query=args.keep_query, relative=args.relative,
                  gone=args.gone, threshold=args.threshold, host=args.host)
    _out(text, args.output)
    for old, why in plan.dropped:
        _err(f"not applied {old}: {why}")
    for w in plan.warnings:
        _err(f"warning: {w}")
    _err(plan.summary())
    return 0


def cmd_verify(args) -> int:
    plan = build_plan(load_csv(args.csv), threshold=args.threshold, host=args.host)
    name, checks = verify(plan, args.base, engine=args.engine, delay=args.delay, timeout=args.timeout,
                          gone=args.gone, relative=args.relative)
    failed = [c for c in checks if not c.ok]
    for c in checks:
        print(f"{'OK  ' if c.ok else 'FAIL'}  {c.url} -> {c.expected}"
              + (f"  ({c.problem})" if c.problem else ""))
        if not c.ok and args.verbose:
            print(f"      {c.render_chain()}")
    print(f"engine: {name}")
    print(f"{len(checks)} checked: {len(checks) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="redirect-map-gen",
                                 description="Build, apply and verify a redirect map for a site migration.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def net_opts(p, delay=1.0):
        p.add_argument("--delay", type=_positive_delay, default=delay,
                       help=f"seconds between requests (default {delay})")
        p.add_argument("--timeout", type=float, default=10.0)

    p = sub.add_parser("urls", help="list the URLs of a sitemap, CSV or text file")
    p.add_argument("source")
    p.add_argument("-o", "--output")
    net_opts(p)
    p.set_defaults(func=cmd_urls)

    p = sub.add_parser("crawl", help="crawl one host politely and list its pages")
    p.add_argument("start")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("-o", "--output")
    net_opts(p)
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("match", help="match old URLs to new URLs and write the review CSV")
    p.add_argument("--old", help="old URLs: sitemap URL/file (.xml, .gz, index), CSV or text list")
    p.add_argument("--old-crawl", metavar="URL", help="crawl the old site instead")
    p.add_argument("--new", help="new URLs: sitemap URL/file, CSV or text list")
    p.add_argument("--new-crawl", metavar="URL", help="crawl the new site instead")
    p.add_argument("--old-base", metavar="URL", help="make relative old paths absolute")
    p.add_argument("--threshold", type=float, default=OK_THRESHOLD,
                   help=f"confidence for status=ok (default {OK_THRESHOLD})")
    p.add_argument("--unmatched", choices=("review", "gone"), default="review",
                   help="no candidate: home suggestion for review (default) or 410 candidate (gone)")
    p.add_argument("--titles", action="store_true",
                   help="fetch page titles for low-confidence pairs (at most --title-limit per side)")
    p.add_argument("--title-limit", type=int, default=TITLE_LIMIT)
    p.add_argument("--max-pages", type=int, default=200, help="crawl page cap (default 200)")
    p.add_argument("-o", "--output")
    net_opts(p)
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("rules", help="turn the (edited) CSV into server rules")
    p.add_argument("csv")
    p.add_argument("--format", "-f", choices=FORMATS, required=True)
    p.add_argument("--threshold", type=float, default=OK_THRESHOLD,
                   help="minimum confidence for a row to become a rule (status=approved always does)")
    p.add_argument("--keep-query", action="store_true", help="pass the query string on to the target")
    p.add_argument("--relative", action="store_true", help="write targets as paths (same host)")
    p.add_argument("--gone", action="store_true", help="also write 410 rules for status=gone")
    p.add_argument("--host", help="host of relative old URLs (required for cloudflare)")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("verify", help="check a live site against the map")
    p.add_argument("csv")
    p.add_argument("--base", required=True, help="live site, e.g. https://example.com")
    p.add_argument("--threshold", type=float, default=OK_THRESHOLD)
    p.add_argument("--host")
    p.add_argument("--engine", choices=("auto", "builtin", "redirect-checker"), default="auto")
    p.add_argument("--gone", action="store_true", help="also expect 410 for status=gone rows")
    p.add_argument("--relative", action="store_true",
                   help="targets are paths on --base (use with rules --relative)")
    p.add_argument("-v", "--verbose", action="store_true", help="print the chain of failures")
    net_opts(p)
    p.set_defaults(func=cmd_verify)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SourceError, RulesError, ImportError, ValueError, OSError) as e:
        _err(f"error: {e}")
        return 2


__all__ = ["main", "build_parser", "pick_engine"]
