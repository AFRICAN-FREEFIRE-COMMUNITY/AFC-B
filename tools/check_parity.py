#!/usr/bin/env python3
"""
tools/check_parity.py - a capability that must exist on BOTH sides, checked, not remembered.

Owner rule R27 (2026-09-13, "fix the model, not the reported record"): a bug arrives as one
record and is almost never one record. The shape is always two surfaces that should behave alike,
one built and the other forgotten. In AFC the pairs are the EVENT flow versus the STANDALONE
leaderboard flow (both read screenshots, take result files, take manual results), and the ADMIN
surface versus the ORGANIZER surface (the organizer page reuses the admin components, or should).

tools/parity_table.json holds the rows: a capability, and for each side a file plus a pattern that
proves the capability is there (a route, a helper call, a component import). A row fails when one
side has its marker and the other does not. Add a row whenever something is built for one side
alone, so the other side is a red line the day it is forgotten, not a bug report a month later.

Files may live in the frontend tree (`frontend:` prefix), which sits beside this repo; a row whose
side cannot be read because the tree is absent is reported as skipped, never as passed.

Usage:
  python tools/check_parity.py [--frontend ../frontend] [--json] [--self-test]
Exit 1 on any one-sided row.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile

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
TABLE = os.path.join(HERE, "parity_table.json")


def resolve(path: str, frontend: str | None) -> str | None:
    if path.startswith("frontend:"):
        if not frontend:
            return None
        return os.path.join(frontend, path[len("frontend:"):])
    return os.path.join(BACKEND, path)


def side_present(side: dict, frontend: str | None) -> bool | None:
    """True / False, or None when the file cannot be read (tree absent)."""
    p = resolve(side["file"], frontend)
    if p is None or not os.path.exists(p):
        return None
    src = io.open(p, encoding="utf-8", errors="ignore").read()
    return re.search(side["pattern"], src) is not None


def check(table: dict, frontend: str | None):
    failures, skipped, ok = [], [], []
    for row in table["rows"]:
        states = {name: side_present(side, frontend) for name, side in row["sides"].items()}
        if any(v is None for v in states.values()):
            skipped.append((row["capability"], [n for n, v in states.items() if v is None]))
            continue
        have = [n for n, v in states.items() if v]
        lack = [n for n, v in states.items() if not v]
        if lack and have:
            failures.append((row["capability"], have, lack))
        elif lack and not have:
            failures.append((row["capability"], [], lack))  # nobody has it: the markers are stale
        else:
            ok.append(row["capability"])
    return ok, failures, skipped


def self_test() -> int:
    """Fixtures both ways: a row present on both sides passes, one-sided fails, an absent tree skips."""
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "a.py"); io.open(a, "w").write("def key_required_response(exc): pass\n")
        b = os.path.join(td, "b.py"); io.open(b, "w").write("return key_required_response(exc)\n")
        c = os.path.join(td, "c.py"); io.open(c, "w").write("nothing here\n")
        rel = lambda p: os.path.relpath(p, BACKEND)  # noqa: E731
        table = {"rows": [
            {"capability": "both", "sides": {"events": {"file": rel(a), "pattern": r"key_required_response"},
                                              "standalone": {"file": rel(b), "pattern": r"key_required_response"}}},
            {"capability": "one-sided", "sides": {"events": {"file": rel(a), "pattern": r"key_required_response"},
                                                   "standalone": {"file": rel(c), "pattern": r"key_required_response"}}},
            {"capability": "tree-absent", "sides": {"events": {"file": rel(a), "pattern": r"key_required_response"},
                                                     "organizer": {"file": "frontend:app/x.tsx", "pattern": r"x"}}},
        ]}
        ok, fails, skipped = check(table, None)
        say = lambda cond, msg: (print(f"  {'ok  ' if cond else 'MISS'} {msg}"), cond)[1]  # noqa: E731
        failures += 0 if say(ok == ["both"], "a row present on both sides passes") else 1
        failures += 0 if say([f[0] for f in fails] == ["one-sided"], "a one-sided row fails") else 1
        failures += 0 if say([s[0] for s in skipped] == ["tree-absent"], "an absent tree is skipped, never passed") else 1
    # the real table: every side's file must exist where the tree is present
    table = json.load(io.open(TABLE, encoding="utf-8"))
    for row in table["rows"]:
        for name, side in row["sides"].items():
            p = resolve(side["file"], FRONTEND)
            if p is not None and not os.path.exists(p):
                failures += 1
                print(f"  MISS {row['capability']} / {name}: file not found {side['file']}")
    print(f"self-test: {failures} failures")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in ARGS:
        return self_test()
    table = json.load(io.open(TABLE, encoding="utf-8"))
    ok, fails, skipped = check(table, FRONTEND)
    if "--json" in ARGS:
        print(json.dumps({"ok": len(ok), "failures": [{"capability": c, "have": h, "lack": l} for c, h, l in fails],
                          "skipped": [{"capability": c, "sides": s} for c, s in skipped]}))
        return 1 if fails else 0
    for cap, have, lack in fails:
        print(f"PARITY {cap}: built on {', '.join(have) or 'no side'}, missing on {', '.join(lack)}")
    for cap, sides in skipped:
        print(f"skipped {cap}: tree absent for {', '.join(sides)}")
    print(f"check-parity: {len(ok)} rows on both sides, {len(fails)} one-sided, {len(skipped)} skipped")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
