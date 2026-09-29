"""Path normalization: accents, case, extensions, index pages, stop words, language prefixes."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit

EXTENSIONS = (".html", ".htm", ".php", ".asp", ".aspx", ".shtml", ".jsp", ".cfm")
INDEX_NAMES = ("index", "default")

# ISO 639-1 codes that sites commonly use as a first path segment (/es/, /fr-ca/).
LANGUAGES = frozenset(
    "en fr es de it pt nl pl ru ja zh ko ar sv da no nb fi cs sk tr el he hu ro uk ca eu gl "
    "hr sl bg sr lt lv et id ms th vi hi bn fa ur".split()
)
_LANG_RE = re.compile(r"^([a-z]{2})(?:[-_][a-z]{2,4})?$")

STOP_WORDS = frozenset(
    # English
    "a an the and or of to in on at for with by from is are be as it its this that our your my "
    # French
    "le la les l un une des du de d et ou au aux en pour par sur dans avec sans ce cette ces "
    "mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs qui que "
    # Spanish
    "el los las lo uno unos unas y e o u del al para por con sin sobre entre mi mis tu tus su sus "
    "nuestro nuestra nuestros nuestras vuestro vuestra se".split()
)

_SPLIT_RE = re.compile(r"[-_.\s+~,]+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def path_of(url: str) -> str:
    """The decoded path of a URL or of a bare path."""
    return unquote(urlsplit(url).path or "/")


def segments(url: str) -> list[str]:
    """Lowercase, accent-free path segments; extension and index page removed; _ becomes -."""
    raw = path_of(url)
    segs = [s for s in raw.split("/") if s]
    out = []
    for i, seg in enumerate(segs):
        s = strip_accents(seg).lower()
        if i == len(segs) - 1:
            for ext in EXTENSIONS:
                if s.endswith(ext) and len(s) > len(ext):
                    s = s[: -len(ext)]
                    break
            if s in INDEX_NAMES:
                continue
        s = re.sub(r"[_\s]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        if s:
            out.append(s)
    return out


def split_lang(segs: list[str]) -> tuple[str | None, list[str]]:
    """('es', rest) when the first segment is a language code like es, fr-ca, pt_br."""
    if segs:
        m = _LANG_RE.match(segs[0])
        if m and m.group(1) in LANGUAGES:
            return m.group(1), segs[1:]
    return None, segs


def words(seg: str) -> list[str]:
    toks = [t for t in _SPLIT_RE.split(seg) if t]
    kept = [t for t in toks if t not in STOP_WORDS]
    return kept or toks


class PathInfo:
    """Everything the matcher needs about one URL, computed once."""

    __slots__ = ("url", "path", "query", "lang", "rest", "slug", "last", "tokens", "joined")

    def __init__(self, url: str) -> None:
        self.url = url
        p = urlsplit(url)
        self.path = unquote(p.path or "/")
        self.query = p.query
        self.lang, rest = split_lang(segments(url))
        self.rest = ["-".join(words(s)) for s in rest]
        self.slug = "/" + "/".join(([self.lang] if self.lang else []) + self.rest)
        self.last = self.rest[-1] if self.rest else ""
        self.tokens = [t for s in self.rest for t in s.split("-") if t]
        self.joined = "/".join(self.rest)

    def __repr__(self) -> str:
        return f"PathInfo({self.url!r}, slug={self.slug!r})"


def slug(url: str) -> str:
    """Comparable form: '/cafe-menu' for '/Caf%C3%A9_Menu.html', '/es' for '/es/index.php'."""
    return PathInfo(url).slug


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def lang_factor(a: str | None, b: str | None) -> float:
    if a == b:
        return 1.0
    if a is None or b is None:
        return 0.85
    return 0.5


def path_similarity(a: PathInfo, b: PathInfo) -> float:
    """0.6 x token Jaccard + 0.4 x difflib ratio, times a language factor (0.5 across languages)."""
    if not a.tokens or not b.tokens:
        return 0.0
    j = jaccard(a.tokens, b.tokens)
    r = SequenceMatcher(None, a.joined, b.joined, autojunk=False).ratio()
    return (0.6 * j + 0.4 * r) * lang_factor(a.lang, b.lang)
