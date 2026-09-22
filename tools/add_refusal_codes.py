"""tools/add_refusal_codes.py - give every 4xx refusal a code the frontend can translate (R35/R44).

WHY
---
`tools/check_refusal_codes.py` counts the refusals that carry a sentence but no code. On 2026-09-17
that was 2,844. The frontend can only translate a refusal that carries a `code`, so each of those is
a sentence that reaches a French or Portuguese player in English. Writing 2,800 codes by hand is not
work anybody should do, and doing it by hand is how the codes end up inconsistent.

WHAT IT DOES
------------
For every `Response({...}, status=4xx)` whose body has no code, it derives one FROM THE MESSAGE and
appends `"code": "<derived>"` to the body. The same sentence always yields the same code, so
"Team not found." is `team_not_found` everywhere it appears and the frontend translates it once.

    {"message": "You are not a member of this team."}   ->  "not_member_team"
    {"message": f"Stage {n} is already seeded."}        ->  "stage_already_seeded"
    {"message": "Invalid or expired session token."}    ->  "invalid_expired_session_token"

Derivation: lowercase the literal, drop punctuation and the placeholders of an f-string, drop the
words that carry no meaning, keep the first four that remain. A one-word result is qualified by the
handler it came from ("unauthorized" alone cannot be translated usefully). A body whose message is a
variable takes the handler name plus `_refused`.

TWO THINGS IT LEARNED THE HARD WAY (2026-09-22, before anything was committed)
-----------------------------------------------------------------------------
1. The code is appended at the END of the body, never after the message match. A message containing
   an escaped quote ("stance must be 'fan'.") ends the match early, and inserting there put the new
   key INSIDE the string: 32 files stopped parsing.
2. Every changed file is parsed BEFORE it is written. A regex pass over 90 files has no business
   trusting itself; a file whose transformation does not parse is skipped and named.

LEFT ALONE: a body that already has a code, a body with nested braces (the counter cannot see those
either), tests, migrations and this tools/ directory.

USE
---
    python tools/add_refusal_codes.py            dry run: counts, samples, nothing written
    python tools/add_refusal_codes.py --apply    write
    python tools/check_refusal_codes.py          the count afterwards

Re-runnable: a second run finds nothing. Pairs with afc_auth/api_errors.py (R79), which covers the
500s; this covers the deliberate refusals.
"""

import argparse
import ast
import io
import os
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The same shape check_refusal_codes.py counts: a flat body, a 4xx status.
PATTERN = re.compile(
    r'Response\(\s*\{(?P<body>[^{}]*)\}\s*,\s*status\s*=\s*(?:status\.HTTP_)?(?P<status>4\d\d)', re.S
)
# The message literal, escaped quotes and all.
MESSAGE = re.compile(
    r'["\']message["\']\s*:\s*f?(?:"(?P<dq>(?:[^"\\]|\\.)*)"|\'(?P<sq>(?:[^\'\\]|\\.)*)\')'
)
PLACEHOLDER = re.compile(r"\{[^}]*\}")

STOP = {
    "a", "an", "the", "you", "your", "yours", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "to", "of", "for", "and", "or", "in", "on", "at", "it", "its", "we", "us",
    "our", "please", "try", "again", "do", "does", "did", "have", "has", "had", "with", "from",
    "there", "their", "they", "them", "as", "by", "can", "may", "will", "would", "should", "must",
    "any", "all", "one", "only", "own", "yet", "still", "now", "so", "if", "when", "while", "than",
    "then", "some", "more", "most", "other", "another", "each", "every", "no", "nor",
}
KEEP = {"not", "no"}          # short, but they carry the meaning


def slug_from_message(text):
    text = PLACEHOLDER.sub(" ", text or "")
    words = re.findall(r"[A-Za-z]+", text.lower())
    picked = [w for w in words if w in KEEP or (len(w) > 2 and w not in STOP)]
    if not picked:
        return ""
    return "_".join(picked[:4])[:40].strip("_")


def enclosing_function(text, index):
    hits = re.findall(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text[:index], re.M)
    return hits[-1] if hits else ""


def is_skipped(path):
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    base = os.path.basename(path)
    return (
        "/migrations/" in rel
        or base.startswith("test_")
        or base.startswith("tests_")
        or base == "tests.py"
        or rel.startswith("tools/")
        or "/.venv/" in rel
        or rel.startswith(".venv/")
    )


def process(text):
    """(new_text, added, codes) - pure, writes nothing."""
    out, last, added, codes = [], 0, 0, []
    for m in PATTERN.finditer(text):
        body = m.group("body")
        if '"code"' in body or "'code'" in body:
            continue
        msg = MESSAGE.search(body)
        literal = None
        if msg:
            literal = msg.group("dq") if msg.group("dq") is not None else msg.group("sq")
        code = slug_from_message(literal) if literal is not None else ""
        func = enclosing_function(text, m.start())
        if code and "_" not in code and func:
            code = ("%s_%s" % (func, code))[:40].strip("_")
        if not code:
            code = ("%s_refused" % func) if func else ""
        if not code:
            continue
        insert_at = m.end("body")
        tail = text[m.start("body"):insert_at]
        lead = "" if tail.rstrip().endswith(",") else ","
        out.append(text[last:insert_at])
        out.append('%s "code": "%s"' % (lead, code))
        last = insert_at
        added += 1
        codes.append(code)
    out.append(text[last:])
    return "".join(out), added, codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the files (default is a dry run)")
    args = ap.parse_args()

    total, files_touched = 0, 0
    all_codes = Counter()
    samples, broken = [], []

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "node_modules", "__pycache__")]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            if is_skipped(path):
                continue
            text = io.open(path, encoding="utf-8", newline="").read()
            if "Response(" not in text:
                continue
            new_text, added, codes = process(text)
            if not added:
                continue
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            try:
                ast.parse(new_text)
            except SyntaxError as exc:
                broken.append((rel, str(exc)[:70]))
                continue
            total += added
            files_touched += 1
            all_codes.update(codes)
            if len(samples) < 16:
                samples.append((rel, codes[0]))
            if args.apply:
                io.open(path, "w", encoding="utf-8", newline="").write(new_text)

    print("%s %d refusals across %d files" % ("coded" if args.apply else "would code", total, files_touched))
    print("distinct codes: %d" % len(all_codes))
    print("the ten most common:")
    for code, n in all_codes.most_common(10):
        print("   %-42s %d" % (code, n))
    print("samples:")
    for rel, code in samples:
        print("   %-52s %s" % (rel, code))
    if broken:
        print("SKIPPED because the change did not parse (nothing written for these):")
        for rel, err in broken:
            print("   %-52s %s" % (rel, err))
    if not args.apply:
        print("dry run: nothing written. Re-run with --apply.")


main()
