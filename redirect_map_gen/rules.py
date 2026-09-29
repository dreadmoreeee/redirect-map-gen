"""Read the review CSV, pick the rows to apply, collapse chains, and write server rules."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from .match import Match

COLUMNS = ["old_url", "new_url", "confidence", "method", "status", "notes"]
FORMATS = ("caddy", "nginx", "nginx-map", "apache", "netlify", "cloudflare")
APPROVED = ("approved", "approve", "yes", "force")
ACTIVE = ("ok", "review", "")
GONE = ("gone", "410")

_SAFE_PATH = "/:@!$&'()*+,;=-._~"
_SAFE_QUERY = _SAFE_PATH + "?"


class RulesError(Exception):
    """Bad CSV or a format that cannot express the input."""


# ---------------------------------------------------------------- CSV

def write_csv(rows, fh) -> None:
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(COLUMNS)
    for m in rows:
        w.writerow([m.old_url, m.new_url, f"{m.confidence:.2f}", m.method, m.status, m.notes])


def to_csv(rows) -> str:
    buf = io.StringIO()
    write_csv(rows, buf)
    return buf.getvalue()


def read_csv(fh) -> list[Match]:
    text = fh.read() if hasattr(fh, "read") else fh
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or "old_url" not in [f.strip() for f in reader.fieldnames]:
        raise RulesError("CSV needs a header with at least old_url,new_url")
    rows = []
    for i, r in enumerate(reader, start=2):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        old = r.get("old_url", "")
        if not old or old.startswith("#"):
            continue
        try:
            conf = float(r.get("confidence") or 1.0)
        except ValueError as e:
            raise RulesError(f"line {i}: confidence {r.get('confidence')!r} is not a number") from e
        rows.append(Match(old, r.get("new_url", ""), conf, r.get("method", ""),
                          r.get("status", "").lower(), r.get("notes", "")))
    return rows


def load_csv(path: str) -> list[Match]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return read_csv(fh.read())


# ---------------------------------------------------------------- plan

@dataclass
class Redirect:
    source: str      # old URL as written in the CSV
    target: str      # final target after chain resolution
    confidence: float
    via: list[str] = field(default_factory=list)  # intermediate hops that were collapsed


@dataclass
class Plan:
    redirects: list[Redirect] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (old_url, reason)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        chains = sum(1 for r in self.redirects if r.via)
        reasons: dict[str, int] = {}
        for _, why in self.dropped:
            key = why.split(":", 1)[0]
            reasons[key] = reasons.get(key, 0) + 1
        parts = [f"{len(self.redirects)} redirect(s)", f"{chains} chain(s) collapsed", f"{len(self.gone)} gone"]
        parts += [f"{n} {k}" for k, n in sorted(reasons.items())]
        return ", ".join(parts)


def _key(url: str, host: str | None) -> tuple[str | None, str, str]:
    p = urlsplit(url.strip())
    h = (p.hostname or "").lower() or (host.lower() if host else None)
    return h, unquote(p.path or "/"), p.query


def _same(a: tuple, b: tuple) -> bool:
    return a[1:] == b[1:] and (a[0] is None or b[0] is None or a[0] == b[0])


def build_plan(rows: list[Match], threshold: float = 0.7, host: str | None = None) -> Plan:
    """Select rows (status approved, or ok/review with confidence >= threshold; gone -> 410),
    then resolve chains A->B->C to A->C and drop self-redirects and loops.

    `host` is the host of relative old URLs. When it is not given, a relative old URL is
    considered to be on the same host as any target with the same path.
    """
    plan = Plan()
    chosen: list[Match] = []
    seen: set[tuple] = set()
    for m in rows:
        st = m.status.lower()
        k = _key(m.old_url, host)
        if st in GONE:
            if any(_same(k, s) for s in seen):
                plan.dropped.append((m.old_url, "duplicate: old URL listed twice"))
                continue
            seen.add(k)
            plan.gone.append(m.old_url)
            continue
        if not (st in APPROVED or (st in ACTIVE and m.confidence >= threshold)):
            why = "below-threshold" if st in ACTIVE else f"status-{st}"
            plan.dropped.append((m.old_url, f"{why}: status={st or '-'} confidence={m.confidence:.2f}"))
            continue
        if not m.new_url:
            plan.dropped.append((m.old_url, "no-target: new_url is empty"))
            continue
        if any(_same(k, s) for s in seen):
            plan.dropped.append((m.old_url, "duplicate: old URL listed twice"))
            continue
        seen.add(k)
        chosen.append(m)

    # Self-redirects are never followed as a hop of someone else's chain.
    sources = [(_key(m.old_url, host), m) for m in chosen
               if not _same(_key(m.old_url, host), _key(m.new_url, None))]

    def next_hop(url: str) -> Match | None:
        k = _key(url, None)
        for sk, m in sources:
            if _same(sk, k):
                return m
        return None

    gone_keys = [_key(g, host) for g in plan.gone]
    for m in chosen:
        start = _key(m.old_url, host)
        target, via, loop = m.new_url, [], False
        visited = [start]
        while True:
            tk = _key(target, None)
            if any(_same(tk, v) for v in visited):
                loop = bool(via)
                break
            nxt = next_hop(target)
            if nxt is None:
                break
            visited.append(_key(nxt.old_url, host))
            via.append(target)
            target = nxt.new_url
        tk = _key(target, None)
        if _same(tk, start) and not via:
            plan.dropped.append((m.old_url, "self-redirect: target is the same URL"))
            continue
        if loop or _same(tk, start):
            plan.dropped.append((m.old_url, "loop: " + " -> ".join([m.old_url] + via + [target])))
            continue
        if any(_same(tk, g) for g in gone_keys):
            plan.warnings.append(f"{m.old_url} redirects to {target}, which is marked gone")
        plan.redirects.append(Redirect(m.old_url, target, m.confidence, via))
    return plan


# ---------------------------------------------------------------- formatting helpers

def _enc_path(path: str) -> str:
    return quote(unquote(path), safe=_SAFE_PATH)


def _target(url: str, relative: bool) -> str:
    """Percent-encoded target; path-only when `relative` is set."""
    p = urlsplit(url.strip())
    path = _enc_path(p.path or "/")
    query = f"?{quote(unquote(p.query), safe=_SAFE_QUERY)}" if p.query else ""
    frag = f"#{quote(unquote(p.fragment), safe=_SAFE_QUERY)}" if p.fragment else ""
    if relative or not p.netloc:
        return path + query + frag
    return f"{p.scheme or 'https'}://{p.netloc}{path}{query}{frag}"


def _src(url: str) -> tuple[str, str]:
    """(decoded path, raw query) of an old URL."""
    p = urlsplit(url.strip())
    return unquote(p.path or "/"), p.query


def _header(fmt: str, plan: Plan, keep_query: bool, threshold: float, comment: str = "#") -> list[str]:
    return [
        f"{comment} Generated by redirect-map-gen: {plan.summary()}.",
        f"{comment} 301, one hop per URL; threshold {threshold:.2f}; query string "
        f"{'kept' if keep_query else 'dropped'}.",
    ]


def _skipped(comment: str, url: str, why: str) -> str:
    return f"{comment} skipped {url}: {why}"


# ---------------------------------------------------------------- caddy

def _caddy_token(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9/._~\-%:@!$&'()+,;=?]+", s) and "{" not in s:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def caddy(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
          threshold: float = 0.7) -> str:
    lines = _header("caddy", plan, keep_query, threshold)
    lines.append("# Paste inside your site block. Caddy path matchers are exact unless they contain *.")
    for r in plan.redirects:
        path, query = _src(r.source)
        if query:
            lines.append(_skipped("#", r.source, "Caddy redir cannot match a query string here"))
            continue
        # Plain paths are matched in decoded form; anything else in escaped (%XX) form.
        src = path if re.fullmatch(r"[A-Za-z0-9/._~\-]+", path) else _enc_path(path)
        tgt = _target(r.target, relative)
        suffix = "{?query}" if keep_query and "?" not in tgt else ""
        lines.append(f"redir {_caddy_token(src)} {_caddy_token(tgt)}{suffix} permanent")
    if gone:
        for g in plan.gone:
            path, query = _src(g)
            if query:
                lines.append(_skipped("#", g, "query string"))
                continue
            src = path if re.fullmatch(r"[A-Za-z0-9/._~\-]+", path) else _enc_path(path)
            lines.append(f"respond {_caddy_token(src)} 410")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- nginx

def _nginx_str(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9/._~\-%:@!&'()*+,=?\x80-\U0010ffff]+", s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _nginx_target(url: str, relative: bool) -> str:
    # "$" would start an nginx variable; it is percent-encoded instead.
    return _target(url, relative).replace("$", "%24")


def is_index_path(path: str) -> bool:
    """/es/index.html and friends: servers map a directory request to this file internally."""
    return bool(re.search(r"/(index|default)\.(html?|php|aspx?|shtml)$", path, re.I))


def _nginx_regex(path: str) -> str:
    bs = chr(92)  # backslash
    return "".join(bs + c if c in ".^$*+?()[]{}|" + bs else c for c in _enc_path(path))


def nginx(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
          threshold: float = 0.7) -> str:
    """location = blocks. An index file (/es/index.html -> /es/) is matched on $request_uri,
    because the index directive re-enters the location internally and would loop."""
    lines = _header("nginx", plan, keep_query, threshold)
    lines.append("# Paste inside the server {} block. nginx matches the decoded path, without the query.")
    for r in plan.redirects:
        path, query = _src(r.source)
        if query:
            lines.append(_skipped("#", r.source, "query string in the source; match $args by hand"))
            continue
        tgt = _nginx_target(r.target, relative)
        # Variables are expanded inside double quotes too, so quoting stays valid.
        tgt = _nginx_str(tgt + "$is_args$args" if keep_query and "?" not in tgt else tgt)
        if is_index_path(path):
            cond = f'if ($request_uri ~ "^{_nginx_regex(path)}(?:\\?|$)") {{ return 301 {tgt}; }}'
            lines.append(f"location = {_nginx_str(path)} {{ {cond} }}")
        else:
            lines.append(f"location = {_nginx_str(path)} {{ return 301 {tgt}; }}")
    if gone:
        for g in plan.gone:
            path, query = _src(g)
            if not query:
                lines.append(f"location = {_nginx_str(path)} {{ return 410; }}")
    return "\n".join(lines) + "\n"


def nginx_map(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
              threshold: float = 0.7) -> str:
    """map blocks (fast with thousands of entries). Keys are the path as the client sent it
    (percent-encoded, from $request_uri), so internal redirects such as index files never
    match again."""
    lines = _header("nginx-map", plan, keep_query, threshold)
    lines += ["# http {} level:",
              "map $request_uri $redirect_map_gen_path {",
              '    "~^(?<p>[^?]*)" $p;',
              "}",
              "map $redirect_map_gen_path $redirect_map_gen_target {",
              "    default \"\";"]
    for r in plan.redirects:
        path, query = _src(r.source)
        if query:
            lines.append("    " + _skipped("#", r.source, "query string in the source"))
            continue
        lines.append(f"    {_nginx_str(_enc_path(path))} {_nginx_str(_nginx_target(r.target, relative))};")
    lines.append("}")
    if gone:
        lines += ["map $redirect_map_gen_path $redirect_map_gen_gone {", "    default 0;"]
        lines += [f"    {_nginx_str(_enc_path(_src(g)[0]))} 1;" for g in plan.gone if not _src(g)[1]]
        lines.append("}")
    lines.append("# server {} level:")
    if gone:
        lines.append("if ($redirect_map_gen_gone) { return 410; }")
    ret = "$redirect_map_gen_target$is_args$args" if keep_query else "$redirect_map_gen_target"
    lines.append(f"if ($redirect_map_gen_target) {{ return 301 {ret}; }}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- apache

_PCRE_SPECIAL = set(".^$*+?()[]{}|\\#")


def apache_regex(s: str) -> str:
    """Escape a literal for a PCRE pattern in an Apache config line (no spaces, bytes as \\xHH)."""
    out = []
    for ch in s:
        if ch in _PCRE_SPECIAL:
            out.append("\\" + ch)
        elif " " < ch < "\x7f":
            out.append(ch)
        else:
            out.extend(f"\\x{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def apache_substitution(s: str) -> str:
    """Escape $ and % (back-references) and \\ in a RewriteRule substitution."""
    return re.sub(r"([\\$%])", r"\\\1", s)


def apache(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
           threshold: float = 0.7) -> str:
    """.htaccess at the document root (per-directory context: patterns have no leading slash).

    RewriteRule rather than 'Redirect 301': Redirect matches every path below the given
    one (/about also catches /about/team) and always appends the query string.
    Every rule only fires on the client's own request (REDIRECT_STATUS empty), so it
    cannot loop with a clean-URL rewrite (/about -> about.html); an index file
    (/es/index.html -> /es/) is also checked against THE_REQUEST, because DirectoryIndex
    maps /es/ back to that file.
    """
    lines = _header("apache", plan, keep_query, threshold)
    lines += ["<IfModule mod_rewrite.c>", "RewriteEngine On"]
    flags = "R=301,L,NE" + ("" if keep_query else ",QSD")
    for r in plan.redirects:
        path, query = _src(r.source)
        tgt = _target(r.target, relative)
        f = flags + (",QSA" if keep_query and "?" in tgt else "")
        lines.append("RewriteCond %{ENV:REDIRECT_STATUS} ^$")
        if is_index_path(path):
            lines.append(f"RewriteCond %{{THE_REQUEST}} ^\\S+\\s{apache_regex(_enc_path(path))}[?\\s]")
        if query:
            lines.append(f"RewriteCond %{{QUERY_STRING}} ^{apache_regex(query)}$")
        lines.append(f"RewriteRule ^{apache_regex(path.lstrip('/'))}$ {apache_substitution(tgt)} [{f}]")
    if gone:
        for g in plan.gone:
            path, query = _src(g)
            if query:
                lines.append(f"RewriteCond %{{QUERY_STRING}} ^{apache_regex(query)}$")
            lines.append(f"RewriteRule ^{apache_regex(path.lstrip('/'))}$ - [G,L]")
    lines.append("</IfModule>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- netlify

def netlify(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
            threshold: float = 0.7) -> str:
    lines = _header("netlify", plan, keep_query, threshold)
    lines.append("# Netlify passes the query string on to 301 targets by itself. A rule is ignored while a")
    lines.append("# file still exists at its path; add ! (301!) to force it.")
    for r in plan.redirects:
        path, query = _src(r.source)
        if query:
            lines.append(_skipped("#", r.source, "query string in the source; write a key=:value rule by hand"))
            continue
        lines.append(f"{_enc_path(path)}  {_target(r.target, relative)}  301")
    if gone and plan.gone:
        lines.append("# Netlify has no plain 410; gone pages fall through to your 404 page:")
        lines += [f"#   {g}" for g in plan.gone]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- cloudflare

def cloudflare(plan: Plan, keep_query: bool = False, relative: bool = False, gone: bool = False,
               threshold: float = 0.7, host: str | None = None, target_host: str | None = None) -> str:
    """Bulk Redirects list CSV: no header row; source without scheme and without query.

    Columns: source_url, target_url, status_code, preserve_query_string,
    include_subdomains, subpath_matching, preserve_path_suffix.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in plan.redirects:
        p = urlsplit(r.source)
        if p.query:
            continue  # Cloudflare source URLs cannot contain a query string
        src_host = p.netloc or host
        if not src_host:
            raise RulesError(f"{r.source}: Cloudflare needs a host; pass --host")
        t = urlsplit(r.target)
        if not t.netloc:
            th = target_host or src_host
            tgt = f"https://{th}{_target(r.target, True)}"
        else:
            tgt = _target(r.target, False)
        w.writerow([f"{src_host}{_enc_path(unquote(p.path or '/'))}", tgt, "301",
                    "TRUE" if keep_query else "FALSE", "FALSE", "FALSE", "FALSE"])
    return buf.getvalue()


RENDERERS = {"caddy": caddy, "nginx": nginx, "nginx-map": nginx_map, "apache": apache,
             "netlify": netlify, "cloudflare": cloudflare}


def render(plan: Plan, fmt: str, keep_query: bool = False, relative: bool = False, gone: bool = False,
           threshold: float = 0.7, host: str | None = None) -> str:
    if fmt not in RENDERERS:
        raise RulesError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    kw = dict(keep_query=keep_query, relative=relative, gone=gone, threshold=threshold)
    if fmt == "cloudflare":
        kw["host"] = host
    return RENDERERS[fmt](plan, **kw)
