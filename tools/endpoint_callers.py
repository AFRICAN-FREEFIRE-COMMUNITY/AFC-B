#!/usr/bin/env python3
"""
tools/endpoint_callers.py - an endpoint needs a caller.

Owner rule R45 (2026-09-13): an endpoint nobody calls is a bug, not a spare part (it rots, it
keeps a permission surface open, and its "fix" never reaches a screen). This script lists every
URL pattern the backend mounts, finds every API path the frontend tree builds, matches them, and
fails on an endpoint with no caller that is not named in tools/endpoint_consumers.json with a
reason (the partner API, the Discord bot, a webhook, an internal command).

Two ways to enumerate the backend, and a self-test that they agree (owner rule R47: a checker that
reads a list out of a source file by regex is reading comments too):
  regex   afc/urls.py includes -> each app's urls.py, comments stripped, `path("...", ...)`
          collected with the mount prefix. Needs nothing installed; runs anywhere.
  django  `--django`: django.setup() and walk get_resolver(); authoritative. Needs the venv.
`--self-test` runs both when Django is importable and asserts the same set; it also proves the
matcher on fixtures both ways.

Frontend calls: every string and template literal in the frontend tree that looks like an API
path (at least one `/`, only path characters, `${...}` collapsed to `*`), after the API base or a
per-module BASE prefix. A fragment matches a pattern when its segments align as a SUFFIX of the
pattern's segments (`*` matches a `<param>` or any literal; a `<param>` matches any fragment
segment). Strict on width: a fragment of one segment only matches a one-segment pattern, so
`run/` cannot claim every `.../run/` endpoint.

Usage (from the backend root; the frontend tree is a sibling checkout):
  python tools/endpoint_callers.py [--frontend ../frontend] [--django] [--json] [--self-test]
Exit 1 on an uncalled endpoint that is not in tools/endpoint_consumers.json.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ARGS = sys.argv[1:]


def opt(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


FRONTEND = opt("--frontend") or next(
    (p for p in (os.path.join(BACKEND, "..", "wt-fe-ocr"), os.path.join(BACKEND, "..", "frontend")) if os.path.isdir(p)),
    None,
)
CONSUMERS = os.path.join(HERE, "endpoint_consumers.json")

# ── comment stripping (Python and TS share enough for this purpose) ─────────────────────────
def strip_py_comments(src: str) -> str:
    # docstrings first: afc/urls.py carries Django's stock module docstring with example
    # `path('', views.home, ...)` lines, which a regex would otherwise count as an endpoint
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    out = []
    for line in src.split("\n"):
        # a `#` outside quotes ends the line
        in_s = None
        buf = []
        i = 0
        while i < len(line):
            ch = line[i]
            if in_s:
                buf.append(ch)
                if ch == "\\":
                    buf.append(line[i + 1] if i + 1 < len(line) else "")
                    i += 2
                    continue
                if ch == in_s:
                    in_s = None
            elif ch in "\"'":
                in_s = ch
                buf.append(ch)
            elif ch == "#":
                break
            else:
                buf.append(ch)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


def strip_ts_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    in_block = in_line = False
    in_str = None
    while i < n:
        ch, nx = src[i], src[i + 1] if i + 1 < n else ""
        if in_block:
            if ch == "*" and nx == "/":
                in_block = False
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if in_line:
            if ch == "\n":
                in_line = False
                out.append("\n")
            i += 1
            continue
        if in_str:
            out.append(ch)
            if ch == "\\":
                out.append(nx)
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == "/" and nx == "*":
            in_block = True
            i += 2
            continue
        if ch == "/" and nx == "/":
            in_line = True
            i += 2
            continue
        if ch in ("\"", "'", "`"):
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)


# ── the backend side ─────────────────────────────────────────────────────────────────────────
PATH_RE = re.compile(r"""\bpath\(\s*(["'])(?P<route>[^"']*)\1\s*,""")
INCLUDE_RE = re.compile(r"""\bpath\(\s*(["'])(?P<prefix>[^"']*)\1\s*,\s*include\(\s*(["'])(?P<module>[^"']+)\3""")
PARAM_RE = re.compile(r"<(?:[a-z_]+:)?([a-zA-Z_]+)>")


def normalize_pattern(route: str) -> str:
    """`organizers/organization/<slug:slug>/ai-key/` -> `organizers/organization/<slug>/ai-key/`."""
    return PARAM_RE.sub(lambda m: f"<{m.group(1)}>", route)


def _matching_bracket(src: str, open_idx: int) -> int:
    """Index of the `]` that closes the `[` at open_idx (strings skipped)."""
    depth, i, in_s = 0, open_idx, None
    while i < len(src):
        ch = src[i]
        if in_s:
            if ch == "\\":
                i += 2
                continue
            if ch == in_s:
                in_s = None
        elif ch in "\"'":
            in_s = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(src)


PATH_HEAD = re.compile(r"""\bpath\(\s*(["'])(?P<route>[^"']*)\1\s*,\s*""")
MODULE_INCLUDE = re.compile(r"""include\(\s*(["'])(?P<module>[^"']+)\1""")


def _routes_in(src: str, prefix: str, found: set[str]) -> None:
    """Walk one urls source: a leaf `path(route, view)` is prefix + route; `path(route, include([...]))`
    recurses into the list with the route added to the prefix; `path(route, include("pkg.urls"))`
    loads that module and recurses. Nested inline includes are what afc_awards/urls.py uses."""
    pos = 0
    while True:
        m = PATH_HEAD.search(src, pos)
        if not m:
            return
        route = prefix + m.group("route")
        after = src[m.end(): m.end() + 12]
        if after.startswith("include(["):
            open_idx = src.index("[", m.end())
            close_idx = _matching_bracket(src, open_idx)
            _routes_in(src[open_idx + 1: close_idx], route, found)
            pos = close_idx
            continue
        if after.startswith("include("):
            mi = MODULE_INCLUDE.match(src, m.end())
            if mi:
                path = os.path.join(BACKEND, *mi.group("module").split(".")) + ".py"
                if os.path.exists(path):
                    _routes_in(strip_py_comments(io.open(path, encoding="utf-8").read()), route, found)
                else:
                    found.add(normalize_pattern(route))  # a third-party include (oauth2_provider): one entry
            pos = m.end()
            continue
        found.add(normalize_pattern(route))
        pos = m.end()


def backend_regex() -> set[str]:
    root = strip_py_comments(io.open(os.path.join(BACKEND, "afc", "urls.py"), encoding="utf-8").read())
    found: set[str] = set()
    _routes_in(root, "", found)
    return found


def backend_django() -> set[str]:
    sys.path.insert(0, BACKEND)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
    import django  # noqa: E402
    django.setup()
    from django.urls import get_resolver  # noqa: E402
    from django.urls.resolvers import URLPattern, URLResolver  # noqa: E402

    found: set[str] = set()

    def walk(patterns, prefix):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                found.add(normalize_pattern(prefix + str(p.pattern)))

    walk(get_resolver().url_patterns, "")
    # Third-party trees the regex side records as ONE entry (django admin, oauth2_provider under
    # sso/), and the DEBUG-only `static()` media patterns (regexes starting with ^), are folded.
    folded: set[str] = set()
    for f in found:
        if "^" in f:
            continue
        if f.startswith("admin/"):
            folded.add("admin/")
        elif f.startswith("sso/") and not any(f == r for r in found if r.count("/") <= 2) and f.count("/") > 2 and "/o/" in f or f.startswith("sso/o/"):
            folded.add("sso/")
        else:
            folded.add(f)
    return folded


# ── the frontend side ────────────────────────────────────────────────────────────────────────
LITERAL_RE = re.compile(r"`([^`]*)`|\"([^\"\\\n]*)\"|'([^'\\\n]*)'")
PATHISH = re.compile(r"^[A-Za-z0-9_\-./*?=&:%{}$\[\]]+$")
SKIP_DIRS = {"node_modules", ".next", ".git", "dist", "build", "out", "coverage", "public", "messages", "scripts"}


def frontend_fragments(root: str) -> set[str]:
    frags: set[str] = set()
    for base in ("app", "lib", "components", "contexts", "hooks"):
        top = os.path.join(root, base)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith((".ts", ".tsx")) or fn.endswith(".d.ts"):
                    continue
                src = strip_ts_comments(io.open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore").read())
                # A module BASE (`const BASE = `${env.NEXT_PUBLIC_BACKEND_API_URL}/sponsors``, or a url()
                # helper built the same way) makes every short literal in that file relative to it:
                # `aGet("mine/")` is sponsors/mine/. The first API-base template in the file wins.
                pm = PREFIX_RE.search(src)
                prefix = pm.group(1).strip("/") if pm else ""
                if prefix:
                    frags.add(f"{prefix}/")  # the module's own root: aGet("") lists the collection
                for m in LITERAL_RE.finditer(src):
                    lit = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
                    if lit is None:
                        continue
                    frag = normalize_fragment(lit) if "/" in lit else (normalize_fragment(lit + "/") if prefix and re.fullmatch(r"[a-z0-9_\-]+", lit) else None)
                    if not frag:
                        continue
                    frags.add(frag)
                    if prefix:
                        if frag.startswith("*/"):
                            frags.add(f"{prefix}/{frag[2:]}")  # `${BASE}/for-event/*/` -> sponsors/for-event/*/
                        elif not lit.startswith(("/", "$", "http")) and "NEXT_PUBLIC" not in lit:
                            frags.add(f"{prefix}/{frag}")  # aGet("mine/") -> sponsors/mine/
    return frags


PREFIX_RE = re.compile(r"NEXT_PUBLIC_BACKEND_API_URL\}/([A-Za-z0-9_\-/]+?)(?:/\$\{|`)")


def normalize_fragment(lit: str) -> str | None:
    s = lit.strip()
    if s.startswith("url(") and s.endswith(")"):
        s = s[4:-1].strip("\"' ")  # a CSS url(...) around an API address (font files)
    if s.startswith("http") and "NEXT_PUBLIC" not in s:
        return None  # an external URL
    s = re.sub(r"\$\{[^}]*NEXT_PUBLIC_BACKEND_API_URL[^}]*\}", "", s)  # the API base itself is not a segment
    s = re.sub(r"\$\{[^}]*\}", "*", s)  # every other interpolation is a wildcard segment
    s = s.split("?", 1)[0].split("#", 1)[0]  # the query string is not part of the address
    s = s.lstrip("/")
    if not s or "/" not in s.rstrip("/") and not s.endswith("/"):
        return None
    if not PATHISH.match(s):
        return None
    if s.startswith(("_next", "static", "media", "images", "fonts", "api/auth")):
        return None
    if not s.endswith("/"):
        s += "/"
    return s


# ── matching ─────────────────────────────────────────────────────────────────────────────────
def segs(s: str) -> list[str]:
    return [x for x in s.strip("/").split("/") if x != ""]


def seg_match(pat_seg: str, frag_seg: str) -> bool:
    if frag_seg == "*" or pat_seg.startswith("<"):
        return True
    return pat_seg == frag_seg


def fragment_matches(pattern: str, fragment: str) -> bool:
    """Strict on width, so a generic tail (`*/edit/`, `run/`) cannot claim every endpoint that
    ends that way: the fragment needs a literal segment, two of them when it starts with a
    wildcard, at least half of the pattern's segments, and one segment only against a
    one-segment pattern."""
    ps, fs = segs(pattern), segs(fragment)
    literal = [x for x in fs if x != "*"]
    if not fs or not literal:
        return False
    # `${BACKEND}/shop/wishlist/toggle/` where BACKEND aliases the API base: the leading wildcard
    # is the base, not a segment. A fragment that starts with a wildcard is tried without it first.
    if fs[0] == "*" and len(fs) > 1 and fragment_matches(pattern, "/".join(fs[1:]) + "/"):
        return True
    if len(fs) > len(ps):
        return False
    if fs[0] == "*" and len(literal) < 2:
        return False
    if len(fs) * 2 < len(ps):
        return False
    if len(fs) == 1 and len(ps) != 1:
        return False
    tail = ps[len(ps) - len(fs):]
    return all(seg_match(p, f) for p, f in zip(tail, fs))


def match(patterns: set[str], fragments: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    # A helper built as `${base}/${path}` called with "" lands on the collection root, so a
    # fragment ending in a wildcard also stands for its parent: `leaderboards/standalone/*/`
    # implies `leaderboards/standalone/`.
    fragments = set(fragments) | {f[:-2] for f in fragments if f.endswith("*/") and f.count("/") >= 2}
    called: dict[str, list[str]] = {}
    for pat in patterns:
        hits = [f for f in fragments if fragment_matches(pat, f)]
        if hits:
            called[pat] = sorted(hits)[:3]
    uncalled = sorted(p for p in patterns if p not in called)
    return called, uncalled


# ── self-test ────────────────────────────────────────────────────────────────────────────────
FIXTURES = [
    # (pattern, fragment, should_match)
    ("organizers/organization/<slug>/ai-key/test/", "organization/*/ai-key/test/", True),
    ("organizers/organization/<slug>/ai-key/test/", "organizers/organization/*/ai-key/test/", True),
    ("shop/view-active-products/", "shop/view-active-products/", True),
    ("leaderboards/standalone/<lb_id>/ocr/jobs/<job_id>/run/", "*/ocr/jobs/*/run/", True),
    ("leaderboards/standalone/<lb_id>/ocr/jobs/<job_id>/run/", "run/", False),
    ("events/ocr-match-result/", "ocr-match-result/", False),  # one segment against two: strict
    ("shop/view-product-details/", "shop/view-product-details/", True),
    ("shop/view-product-details/", "view-product-details/", False),
    ("team/get-team-details/", "team/get-team-details/", True),
    ("team/get-team-details/", "team/get-team/", False),
    ("team/edit-team/", "*/edit/", False),  # a generic tail claims nothing
    ("events/<slug>/edit/", "*/edit/", False),
    ("leaderboards/standalone/<lb_id>/ocr/", "*/ocr/", False),
    ("leaderboards/standalone/<lb_id>/ocr/", "standalone/*/ocr/", True),
    ("organizers/leaderboard-fonts/by-id/<font_id>/file/", "*/organizers/leaderboard-fonts/by-id/*/file/", True),
]


def self_test() -> int:
    failures = 0
    for pat, frag, want in FIXTURES:
        got = fragment_matches(pat, frag)
        if got != want:
            failures += 1
            print(f"  MISS {pat} <- {frag}: expected {want}, got {got}")
    for lit, want in [
        ("${env.NEXT_PUBLIC_BACKEND_API_URL}/shop/view-product-details/?ref=${id}", "shop/view-product-details/"),
        ("${base}/add/", "*/add/"),
        ("url(${BACKEND}/organizers/leaderboard-fonts/by-id/${id}/file/)", "*/organizers/leaderboard-fonts/by-id/*/file/"),
        ("organization/${slug}/ai-key/", "organization/*/ai-key/"),
        ("https://platform.twitter.com/embed/Tweet.html?id=${x}", None),
        ("text-sm text-muted-foreground", None),
        ("/login?redirect=${r}", None),  # a frontend route, not an API path
    ]:
        got = normalize_fragment(lit)
        if got != want:
            failures += 1
            print(f"  MISS normalize {lit!r}: expected {want!r}, got {got!r}")
    regex_set = backend_regex()
    print(f"  regex enumerates {len(regex_set)} endpoints")
    try:
        django_set = backend_django()
        print(f"  django enumerates {len(django_set)} endpoints")
        only_regex = sorted(regex_set - django_set)
        only_django = sorted(django_set - regex_set)
        if only_regex or only_django:
            failures += 1
            print(f"  MISS the two enumerations differ: regex-only {only_regex[:8]} django-only {only_django[:8]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (django enumeration skipped here: {type(exc).__name__}: {str(exc)[:80]})")
    print(f"self-test: {failures} failures")
    return 1 if failures else 0


# ── main ─────────────────────────────────────────────────────────────────────────────────────
def main() -> int:
    if "--self-test" in ARGS:
        return self_test()
    patterns = backend_django() if "--django" in ARGS else backend_regex()
    if not FRONTEND or not os.path.isdir(FRONTEND):
        print("endpoint-callers: skipped, no frontend tree (pass --frontend <path>)")
        return 0
    fragments = frontend_fragments(FRONTEND)
    called, uncalled = match(patterns, fragments)
    consumers = json.load(io.open(CONSUMERS, encoding="utf-8")) if os.path.exists(CONSUMERS) else {}
    named = {k: v for k, v in consumers.items() if not k.startswith("_")}
    unexplained = [p for p in uncalled if p not in named]
    stale_names = sorted(k for k in named if k in called)  # a named consumer that now has a frontend caller
    if "--json" in ARGS:
        print(json.dumps({"endpoints": len(patterns), "fragments": len(fragments), "called": len(called),
                          "uncalled": len(uncalled), "unexplained": unexplained, "stale_names": stale_names}))
        return 1 if unexplained else 0
    for p in unexplained:
        print(f"UNCALLED {p}")
    for p in stale_names:
        print(f"NAMED-BUT-CALLED {p}  ({named[p]})")
    print(f"endpoint-callers: {len(patterns)} endpoints, {len(called)} called from {len(fragments)} frontend paths, "
          f"{len(uncalled)} uncalled of which {len(unexplained)} unexplained")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
