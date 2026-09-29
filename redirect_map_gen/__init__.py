"""redirect-map-gen: build, apply and verify a redirect map for a site migration."""

__version__ = "1.0.0"

from .crawl import CrawlResult, crawl
from .match import Match, clean_titles, fetch_titles_with, match, title_similarity
from .net import USER_AGENT, PoliteFetcher
from .normalize import PathInfo, path_similarity, slug
from .rules import FORMATS, Plan, Redirect, build_plan, load_csv, read_csv, render, to_csv, write_csv
from .sources import load_sitemap, load_urls, parse_list, parse_sitemap
from .verify import pick_engine, verify

__all__ = [
    "CrawlResult", "crawl", "Match", "clean_titles", "fetch_titles_with", "match", "title_similarity",
    "USER_AGENT", "PoliteFetcher", "PathInfo", "path_similarity", "slug", "FORMATS", "Plan",
    "Redirect", "build_plan", "load_csv", "read_csv", "render", "to_csv", "write_csv",
    "load_sitemap", "load_urls", "parse_list", "parse_sitemap", "pick_engine", "verify",
    "__version__",
]
