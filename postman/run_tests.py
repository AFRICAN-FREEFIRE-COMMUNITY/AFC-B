#!/usr/bin/env python
"""
postman/run_tests.py - fire every request in the generated collection at a base URL and
flag every 5xx (a real server bug) vs 4xx (handled validation). This is the headless,
scriptable version of "run the whole Postman collection" - the same sweep that found the
earlier 500 bugs.

USAGE:
    AFC_TEST_TOKEN=<session_token> [AFC_TEST_APIKEY=<key>] \
        .venv/Scripts/python.exe postman/run_tests.py <base_url> <mode>

  base_url : e.g. https://api.africanfreefirecommunity.com  OR  http://127.0.0.1:8000
  mode     : "reads" -> ONLY GET requests are sent (SAFE for production: never writes).
             "all"   -> every method incl. POST/PUT/PATCH/DELETE (LOCAL/clone only).

The token (a SessionToken) is read from the environment, never written to disk or printed.
Writes use the request's placeholder JSON body ({}). 5xx and connection errors are printed
with a short response snippet so the real bugs are obvious.
"""
import os
import sys
import json
import urllib.request
import urllib.error
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, "AFC_Backend_API.postman_collection.json")

base = sys.argv[1].rstrip("/")
mode = sys.argv[2] if len(sys.argv) > 2 else "reads"
token = os.environ.get("AFC_TEST_TOKEN", "")
apikey = os.environ.get("AFC_TEST_APIKEY", "")


def all_requests(items):
    for it in items:
        if "item" in it:
            yield from all_requests(it["item"])
        else:
            yield it


coll = json.load(open(COLLECTION, encoding="utf-8"))
results = []
errors = []

for it in all_requests(coll["item"]):
    r = it["request"]
    method = r["method"].upper()
    # PRODUCTION SAFETY: in "reads" mode never send a write request.
    if mode == "reads" and method != "GET":
        continue

    raw = r["url"]["raw"].replace("{{base_url}}", "")  # -> "/auth/.../"
    url = base + raw

    headers = {}
    for h in r.get("header", []):
        headers[h["key"]] = h["value"].replace("{{token}}", token).replace("{{api_key}}", apikey)

    data = None
    if method in ("POST", "PUT", "PATCH"):
        data = ((r.get("body") or {}).get("raw", "{}") or "{}").encode()
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=25)
        code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        if code >= 500:
            try:
                snippet = e.read().decode(errors="replace")[:180]
            except Exception:
                snippet = ""
            errors.append((code, method, raw, snippet.replace("\n", " ")))
    except Exception as e:
        code = "EXC"
        errors.append(("EXC", method, raw, str(e)[:160]))
    results.append((str(code), method, raw))

cls = Counter((c[0] if c[0] != "EXC" else "EXC") for c, *_ in [(x[0],) for x in results])
print(f"=== {mode.upper()} sweep @ {base} | {len(results)} requests ===")
print("status classes:", dict(Counter(c[0][0] if c[0][0].isdigit() else c[0] for c in [(x[0],) for x in results])))
print(f"--- {len(errors)} server errors (5xx / connection):")
for code, method, raw, snip in errors:
    print(f"  {code} {method} {raw}  ::  {snip}")
