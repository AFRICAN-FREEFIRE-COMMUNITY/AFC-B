#!/usr/bin/env python3
"""check_known_bugs.py - run the registry in tools/known_bugs.json against the tree.

Owner's workflow rule (2026-09-11): a bug met more than once gets a checker; the checker runs on
every push and PR; a red checker is fixed before anything else. This is the backend runner (the
frontend has scripts/check-known-bugs.mjs). Standard library only, sub-second, so it is the first
step of the deploy workflow and of checks.yml, and it can run by hand:

    python tools/check_known_bugs.py            # from the repo root
    backend/.venv/Scripts/python.exe tools/check_known_bugs.py   # from WEBSITE on Windows

Check kinds:
    regex         a pattern (Python re, unicode) that must not appear in the listed globs
    crlf          the file must not contain a carriage return anywhere
    prod-imports  every third-party module the listed files import must be pinned in the entry's
                  `requirements` file (what the server installs). Read with ast, so comments and
                  strings never count; the standard library, the repo's own packages (plus `local`,
                  modules reached through sys.path), imports guarded by `except ImportError` or
                  `except Exception`, and `optional` modules (each with its reason) are skipped. `aliases` maps
                  an import name to its distribution (bs4 -> beautifulsoup4); `implied` names a
                  distribution a source pattern needs although nothing imports it ("lxml" as a
                  BeautifulSoup parser).
An entry may name `except`: files (repo-relative) the pattern is ALLOWED in, for a rule of the
shape "this call exists in exactly these chokepoints and nowhere else".

Output on failure, one line per hit, greppable, then the prescribed fix:
    KNOWN-BUG <id> <file>:<line>  <title>
Exit 1 = red run. Exit 2 = the registry itself is broken.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "tools" / "known_bugs.json"
SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "__pycache__", "media", "static", "staticfiles"}


def files_for(globs: list[str], allowed: list[str] | None = None) -> list[Path]:
    out: set[Path] = set()
    # `except` entries are exact paths, or globs ("**/test*.py") matched against the repo-relative
    # posix path, so a whole class of files (tests that spell an expected address) can be excused.
    allowed = allowed or []
    skip = {ROOT / a for a in allowed if not any(ch in a for ch in "*?[")}
    skip_globs = [a for a in allowed if any(ch in a for ch in "*?[")]
    for g in globs:
        for p in ROOT.glob(g):
            relp = p.relative_to(ROOT)
            if any(relp.match(sg) or fnmatch.fnmatch(relp.as_posix(), sg) for sg in skip_globs):
                continue
            if p.is_file() and p not in skip and not any(part in SKIP_DIRS for part in relp.parts):
                out.add(p)
    return sorted(out)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def check_regex(bug: dict, hits: list) -> None:
    pat = re.compile(bug["pattern"])
    for p in files_for(bug["globs"], bug.get("except")):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary (an image in deploy/), not source
        for i, line in enumerate(text.split("\n"), 1):
            if pat.search(line):
                hits.append((bug, rel(p), i, line.strip()[:100]))


def check_crlf(bug: dict, hits: list) -> None:
    for p in files_for(bug["globs"]):
        data = p.read_bytes()
        if b"\r" in data:
            first = data.split(b"\r", 1)[0].count(b"\n") + 1
            hits.append((bug, rel(p), first, "first carriage return on this line"))


def _dist_key(name: str) -> str:
    """PEP 503 normalised: Pillow, pillow and pillow_heif / pillow-heif compare equal to their pins."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _guarded(node: ast.AST, parents: dict) -> bool:
    """An import inside `try:` whose handler catches ImportError is optional on purpose."""
    child, p = node, parents.get(node)
    while p is not None:
        if isinstance(p, ast.Try) and child in p.body:
            for h in p.handlers:
                names = [h.type] if not isinstance(h.type, ast.Tuple) else list(h.type.elts)
                if h.type is None or any(isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException") for n in names):
                    return True
        child, p = p, parents.get(p)
    return False


def check_prod_imports(bug: dict, hits: list) -> None:
    req = ROOT / bug["requirements"]
    pinned = set()
    for line in req.read_text(encoding="utf-8").splitlines():
        name = re.split(r"[=<>!~;\[ #]", line.strip(), maxsplit=1)[0]
        if name:
            pinned.add(_dist_key(name))
    # `local`: repo modules reached through sys.path (afcbot/afc_scraper.py); `optional`: modules a
    # feature probes for and does without, each with its reason in the registry.
    local = {p.name for p in ROOT.iterdir() if p.is_dir()} | {p.stem for p in ROOT.glob("*.py")} | set(bug.get("local", []))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    aliases = bug.get("aliases", {})
    optional = bug.get("optional", {})
    for p in files_for(bug["globs"], bug.get("except")):
        text = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                if top in stdlib or top in local or top in optional or _guarded(node, parents):
                    continue
                dist = aliases.get(top, top)
                if _dist_key(dist) not in pinned:
                    hits.append((bug, rel(p), node.lineno, f"imports {top} ({dist}), not pinned in {bug['requirements']}"))
        for imp in bug.get("implied", []):
            m = re.search(imp["pattern"], text)
            if m and _dist_key(imp["dist"]) not in pinned:
                line = text[: m.start()].count("\n") + 1
                hits.append((bug, rel(p), line, f"needs {imp['dist']} ({imp['why']}), not pinned in {bug['requirements']}"))


def _dotted(node: ast.AST) -> str:
    """`TeamMembers.objects.create` for the func of a call, "" when it is not a plain dotted name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def check_call_outside_with(bug: dict, hits: list) -> None:
    """A call that must only ever run inside `with <guard>(...)` (inbox #314, 2026-10-18: every
    TeamMembers row is created under _team_seat_lock, or two joins at once can take one seat).
    `call` is the dotted call, `with` the guard's name, `allow_functions` the functions exempt."""
    allowed = set(bug.get("allow_functions", []))

    def guarded_with(node: ast.With) -> bool:
        return any(isinstance(i.context_expr, ast.Call) and _dotted(i.context_expr.func) == bug["with"]
                   for i in node.items)

    def walk(node, func, inside, p):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name, False, p)
                continue
            now = inside or (isinstance(child, ast.With) and guarded_with(child))
            if (isinstance(child, ast.Call) and _dotted(child.func) == bug["call"]
                    and not inside and func not in allowed):
                hits.append((bug, rel(p), child.lineno, f"{bug['call']} in {func or 'module'} outside `with {bug['with']}(...)`"))
            walk(child, func, now, p)

    for p in files_for(bug["globs"], bug.get("except")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        walk(tree, None, False, p)


def main() -> int:
    # Windows consoles default to cp1252 and choke on the very characters some checks look for.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"registry unreadable: {e}")
        return 2
    hits: list = []
    for bug in registry["bugs"]:
        kind = bug.get("kind")
        if kind == "regex":
            check_regex(bug, hits)
        elif kind == "crlf":
            check_crlf(bug, hits)
        elif kind == "prod-imports":
            check_prod_imports(bug, hits)
        elif kind == "call-outside-with":
            check_call_outside_with(bug, hits)
        else:
            print(f"unknown check kind {kind!r} for {bug.get('id')}")
            return 2
    if not hits:
        print(f"known-bugs: {len(registry['bugs'])} checks, 0 hits")
        return 0
    by_id: dict[str, list] = {}
    for h in hits:
        by_id.setdefault(h[0]["id"], []).append(h)
    for bug_id, lst in by_id.items():
        for bug, path, line, detail in lst:
            print(f"KNOWN-BUG {bug_id} {path}:{line}  {bug['title']}  |  {detail}")
        print(f"  fix: {lst[0][0]['fix']}\n")
    print(f"known-bugs: {len(hits)} hit(s) across {len(by_id)} known bug(s). Fix them; do not merge around them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
