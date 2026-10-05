"""Every link the backend puts in front of a person (emails, notifications, Discord posts) must open a
page that exists on the site.

WHY THIS EXISTS (inbox #68, owner, 27 Sep 2026)
    The owner opened the "your account has been deleted" email, pressed "Contact support", and got a
    404: the button said {SITE_URL}/support, a page that never existed. The sweep that followed found
    SEVEN such links live in production at once: /support (both account-deleted emails, since
    17 Sep), /verify (the sign-up code email), /player-market, /applications, /my-invites and
    /team/trials (player market emails), and /static/logo.png (the logo in four tournament emails).
    Nothing connected an email's link to the frontend's pages, so nothing could notice.

WHAT IT READS
    Backend: every .py file (tests and migrations skipped) for links on the site, in three forms:
        f"{SITE_URL}/path", f"{FRONTEND_URL}/path" (and the other base names in BASES), and
        "https://africanfreefirecommunity.com/path" written out in full.
    Frontend (--frontend <tree>): app/**/page.tsx and route.ts give the pages ((group) folders
    dropped, [param] matches one segment, [...rest] the rest), and public/ gives the files.
    A {placeholder} in a backend link counts as one segment; ?query and #fragment are ignored.
    The help bot (inbox #159, 5 Oct 2026): afcbot/bot.py WEB_SITE_PAGES, the pages the website's Help
    panel may link, read as the module's own literal (ast), never by regex. Each path must open a
    page, and a ?tab= / ?subject= value must appear as a string literal in that page's folder (the
    page reads it from the address), so a renamed tab is caught too.

WHAT IS NOT A LINK
    NOT_LINKS names the site URLs that are identifiers, each with its reason. Add one only with a reason.

RUN
    python tools/check_email_links.py --frontend ../wt-fe-x            text report, exit 1 on a dead link
    python tools/check_email_links.py --frontend ../wt-fe-x --json     one JSON line (check-all reads it)
    python tools/check_email_links.py --self-test                      proves it catches and passes

Consumed by the frontend's scripts/check-all.mjs (step "email-links"), which runs where a backend
tree sits beside the frontend, the same way as endpoint_callers.py and check_parity.py.
"""
import argparse
import ast
import json
import os
import re
import sys
import tempfile

DOMAIN = r"https?://(?:www\.)?africanfreefirecommunity\.com"
BASES = ("SITE_URL", "FRONTEND_URL", "settings.FRONTEND_URL", "FRONTEND_BASE_URL", "frontend_url", "site_url")
# Where a link starts: a base placeholder or the domain written out. The path after it is read
# only once every {placeholder} in the rest of the line is replaced by one segment "X": a
# placeholder can hold brackets, commas and quotes ({quote(team, safe='')}), and reading the path
# with those still in it cut it short and checked the wrong address (found by the self-test).
BASE = re.compile(r"\{(?:" + "|".join(re.escape(b) for b in BASES) + r")\}|" + DOMAIN)
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
PATH = re.compile(r"/[^\"'\s)<>]*")
NOT_LINKS = {
    # The SSO back-channel event type: a URI that NAMES the event in a signed token (RFC 8417 style),
    # never opened by anybody (afc_sso/webhooks.py EVENT_TYPE)
    "/secevent/player-disconnected": "SSO security event type identifier, not a page",
}
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "migrations", "__pycache__", "staticfiles", "media"}


def backend_links(root):
    """{clean path: [file:line, ...]} for every site link in the backend's own code."""
    found = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith("wt-")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test") or name == os.path.basename(__file__):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    for m in BASE.finditer(line):
                        rest = PLACEHOLDER.sub("X", line[m.end():])
                        p = PATH.match(rest)
                        if not p:
                            continue            # the bare site address, or a base used some other way
                        clean = p.group(0).split("#")[0].split("?")[0].rstrip(".,;:")
                        found.setdefault(clean or "/", []).append(f"{os.path.relpath(path, root)}:{n}")
    return found


def bot_pages(root):
    """{path as written, ?query included: [file:line]} from afcbot/bot.py WEB_SITE_PAGES."""
    path = os.path.join(root, "afcbot", "bot.py")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "WEB_SITE_PAGES" for t in node.targets):
            for item in node.value.elts:
                _name, page = ast.literal_eval(item)
                found.setdefault(page, []).append(f"afcbot/bot.py:{item.lineno}")
    return found


def page_folder(tree, clean):
    """The app/ folder whose page answers this path (static segments only), or None."""
    app = os.path.join(tree, "app")
    want = [s for s in clean.split("/") if s]
    for dirpath, dirs, files in os.walk(app):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        if not {"page.tsx", "page.ts", "page.jsx"} & set(files):
            continue
        rel = os.path.relpath(dirpath, app).replace("\\", "/")
        segs = [p for p in rel.split("/") if p not in (".", "") and not (p.startswith("(") and p.endswith(")"))]
        if segs == want:
            return dirpath
    return None


def query_values_read(tree, page):
    """True when every ?key=value of a bot page is a string literal in the page's folder."""
    if "?" not in page:
        return True
    clean, query = page.split("?", 1)
    folder = page_folder(tree, clean or "/")
    if not folder:
        return False
    text = ""
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            if name.endswith((".tsx", ".ts")):
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                    text += fh.read()
    return all(f'"{value}"' in text and f'"{key}"' in text
               for key, _eq, value in (pair.partition("=") for pair in query.split("&")))


def frontend_pages(tree):
    """(route segment lists, public file paths) of the frontend tree."""
    routes, public = [], set()
    app = os.path.join(tree, "app")
    for dirpath, dirs, files in os.walk(app):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        if {"page.tsx", "page.ts", "page.jsx", "route.ts"} & set(files):
            rel = os.path.relpath(dirpath, app).replace("\\", "/")
            routes.append([p for p in rel.split("/") if p not in (".", "") and not (p.startswith("(") and p.endswith(")"))])
    pub = os.path.join(tree, "public")
    for dirpath, _dirs, files in os.walk(pub):
        for name in files:
            public.add("/" + os.path.relpath(os.path.join(dirpath, name), pub).replace("\\", "/"))
    return routes, public


def opens(path, routes, public):
    if path in public or path.rstrip("/") in public:
        return True
    segs = [s for s in path.split("/") if s]
    for r in routes:
        if r and r[-1].startswith("[..."):
            if len(segs) >= len(r) - (1 if r[-1].startswith("[[...") else 0) and all(
                    p.startswith("[") or p == segs[i] for i, p in enumerate(r[:-1])):
                return True
            continue
        if len(r) != len(segs):
            continue
        if all(p.startswith("[") or p == segs[i] for i, p in enumerate(r)):
            return True
    return False


def check(backend, frontend):
    links = backend_links(backend)
    routes, public = frontend_pages(frontend)
    dead = {p: w for p, w in links.items() if p not in NOT_LINKS and not opens(p, routes, public)}
    for page, where in bot_pages(backend).items():
        links.setdefault(page, []).extend(where)
        if not opens(page.split("?")[0] or "/", routes, public) or not query_values_read(frontend, page):
            dead[page] = where
    return links, dead


def self_test():
    """Catches a dead link, passes a live one, a [param] page, a public file and a NOT_LINKS entry."""
    with tempfile.TemporaryDirectory() as tmp:
        be, fe = os.path.join(tmp, "be"), os.path.join(tmp, "fe")
        for d in ("app/(user)/contact", "app/(user)/teams/[id]/applications", "public"):
            os.makedirs(os.path.join(fe, d))
        for d in ("app/(user)/contact", "app/(user)/teams/[id]/applications"):
            open(os.path.join(fe, d, "page.tsx"), "w").write(
                'export default function P(){const t = useAddressTab("tab", ["faq"], "faq"); return null}')
        open(os.path.join(fe, "public", "logo.png"), "wb").write(b"x")
        os.makedirs(os.path.join(be, "app"))
        open(os.path.join(be, "app", "emails.py"), "w").write(
            'a = f"{SITE_URL}/contact"\n'
            'b = f"{SITE_URL}/support"\n'
            'c = f"https://africanfreefirecommunity.com/teams/{quote(t)}/applications"\n'
            'd = "https://africanfreefirecommunity.com/logo.png"\n'
            'e = "https://africanfreefirecommunity.com/secevent/player-disconnected"\n'
            'f = "https://africanfreefirecommunity.com/team/trials"\n')
        os.makedirs(os.path.join(be, "afcbot"))
        open(os.path.join(be, "afcbot", "bot.py"), "w").write(
            'WEB_SITE_PAGES = (\n'
            '    ("Contact", "/contact"),\n'
            '    ("FAQ", "/contact?tab=faq"),\n'
            '    ("Gone", "/gone"),\n'
            '    ("Renamed tab", "/contact?tab=old"),\n'
            ')\n')
        links, dead = check(be, fe)
    want_dead = {"/support", "/team/trials", "/gone", "/contact?tab=old"}
    ok = set(dead) == want_dead and len(links) == 9
    print("self-test ok: 4 dead caught (2 email, 2 help bot), 5 good passed" if ok
          else f"self-test FAIL: dead {sorted(dead)}, links {sorted(links)}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frontend", help="the frontend tree (AFC_Frontend checkout)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.frontend or not os.path.isdir(os.path.join(args.frontend, "app")):
        print("give --frontend <AFC_Frontend tree>", file=sys.stderr)
        return 2
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    links, dead = check(backend, args.frontend)
    if args.json:
        print(json.dumps({"links": len(links), "dead": [{"path": p, "where": w} for p, w in sorted(dead.items())]}))
    else:
        for p, w in sorted(dead.items()):
            print(f"DEAD-LINK {p}  <- {', '.join(w[:4])}{' (+%d)' % (len(w) - 4) if len(w) > 4 else ''}")
        print(f"{len(links)} site links, {len(dead)} lead nowhere")
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
