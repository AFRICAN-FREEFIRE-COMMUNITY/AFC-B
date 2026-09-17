"""
tools/check_refusal_codes.py - count the 4xx refusals that carry a sentence but no code.

WHY (owner rule R35, ledgered under R32, 2026-09-17). A refusal is `Response({"message": ...},
status=4xx)`. The frontend can only translate one when it carries a `code`; a bare sentence is
shown as the server wrote it, in English, whatever language the person chose (R44). On
2026-09-17 there were 2,844 such refusals across the backend and 53 with a code. That number is
not fixed in one pass; it is ledgered, so it can only fall: the frontend's check-all reads this
tool's JSON into scripts/debt-ledger.json as `refusals.uncoded`, a commit that raises it fails,
and a fall is written back.

WHAT COUNTS. Every `Response({...}, status=4xx)` (or `status=status.HTTP_4xx_*`) whose dict
literal names "message" and not "code". Test files and migrations are skipped. A refusal built
from a variable (`Response(payload, status=400)`) is not seen; that is a known blind spot, the
same one the endpoint-callers tool has, and it is stated here rather than hidden.

USAGE
    python tools/check_refusal_codes.py            the count and the ten heaviest files
    python tools/check_refusal_codes.py --json     {"uncoded": N, "coded": M, "files": {...}}
    python tools/check_refusal_codes.py --self-test  the pattern fires on a fixture and not on a coded one
"""
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERN = re.compile(
    r'Response\(\s*\{(?P<body>[^{}]*)\}\s*,\s*status\s*=\s*(?:status\.HTTP_)?(?P<code>4\d\d)', re.S
)


def _is_test(path):
    name = os.path.basename(path)
    return name.startswith("test") or name.startswith("tests")


def scan(root=ROOT):
    uncoded, coded, files = 0, 0, {}
    for path in glob.glob(os.path.join(root, "afc_*", "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if "/migrations/" in rel or _is_test(rel):
            continue
        text = io.open(path, encoding="utf-8", errors="ignore").read()
        for m in PATTERN.finditer(text):
            body = m.group("body")
            if '"message"' not in body and "'message'" not in body:
                continue
            if '"code"' in body or "'code'" in body:
                coded += 1
            else:
                uncoded += 1
                files[rel] = files.get(rel, 0) + 1
    return {"uncoded": uncoded, "coded": coded, "files": dict(sorted(files.items(), key=lambda kv: -kv[1]))}


def self_test():
    fixture = '''
def v(request):
    if bad:
        return Response({"message": "No."}, status=status.HTTP_400_BAD_REQUEST)
    if worse:
        return Response({"message": "No.", "code": "no"}, status=403)
    return Response({"ok": True}, status=200)
'''
    hits = [m for m in PATTERN.finditer(fixture)]
    un = [m for m in hits if '"code"' not in m.group("body")]
    ok = len(hits) == 2 and len(un) == 1
    print("self-test:", "OK" if ok else "FAILED", f"({len(hits)} refusals seen, {len(un)} uncoded)")
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    result = scan()
    if "--json" in sys.argv:
        print(json.dumps(result))
        sys.exit(0)
    print(f"refusals without a code: {result['uncoded']} (with a code: {result['coded']})")
    for rel, n in list(result["files"].items())[:10]:
        print(f"  {n:5d}  {rel}")
