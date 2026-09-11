#!/usr/bin/env python3
"""check_known_bugs.py - run the registry in tools/known_bugs.json against the tree.

Owner's workflow rule (2026-09-11): a bug met more than once gets a checker; the checker runs on
every push and PR; a red checker is fixed before anything else. This is the backend runner (the
frontend has scripts/check-known-bugs.mjs). Standard library only, sub-second, so it is the first
step of the deploy workflow and of checks.yml, and it can run by hand:

    python tools/check_known_bugs.py            # from the repo root
    backend/.venv/Scripts/python.exe tools/check_known_bugs.py   # from WEBSITE on Windows

Check kinds:
    regex   a pattern (Python re, unicode) that must not appear in the listed globs
    crlf    the file must not contain a carriage return anywhere

Output on failure, one line per hit, greppable, then the prescribed fix:
    KNOWN-BUG <id> <file>:<line>  <title>
Exit 1 = red run. Exit 2 = the registry itself is broken.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "tools" / "known_bugs.json"
SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "__pycache__", "media", "static", "staticfiles"}


def files_for(globs: list[str]) -> list[Path]:
    out: set[Path] = set()
    for g in globs:
        for p in ROOT.glob(g):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
                out.add(p)
    return sorted(out)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def check_regex(bug: dict, hits: list) -> None:
    pat = re.compile(bug["pattern"])
    for p in files_for(bug["globs"]):
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
