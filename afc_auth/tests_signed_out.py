"""
afc_auth/tests_signed_out.py - the API refuses anonymously, and the set of endpoints that answer
a stranger can only shrink.

Owner rules R25 + R26 (2026-09-13): content is public, the action is gated, and the interface
hiding a control is a courtesy; the API is what stops anybody. This test walks EVERY mounted
endpoint (the same enumeration tools/endpoint_callers.py proves against the regex one), calls it
with no token as GET and as POST with an empty body, and compares the endpoints that answered
2xx against tools/anonymous_answers.json, the recorded set of endpoints that are public by
design (a tournament page, the shop list, a leaderboard).

  - An endpoint that answers a stranger 2xx and is NOT in the file fails the test: a new public
    door has to be a decision, written into the file with the commit that opened it.
  - An endpoint in the file that no longer answers 2xx is reported as stale (not a failure: it
    got gated, the file should lose the line).
  - A 500 is recorded as "error" and listed: the route blew up on an anonymous call, which is a
    developer error on screen (owner rule R44), and each one is worth a look.

Regenerate the file deliberately: WRITE_ANON_BASELINE=1 python manage.py test afc_auth.tests_signed_out
"""
import io
import json
import os
import uuid

from django.test import Client, TestCase
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

BASELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "anonymous_answers.json")
SKIP_PREFIXES = ("admin/", "sso/o/", "sso/.well-known/", "sso/applications/", "sso/authorized_tokens/")
DUMMY = {"int": "1", "slug": "x", "str": "x", "uuid": str(uuid.UUID(int=1)), "path": "x", None: "x"}


def all_endpoints():
    """Every concrete pattern with its params filled: [(pattern, concrete_path)]."""
    out = []

    def walk(patterns, prefix):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                pat = prefix + str(p.pattern)
                if "^" in pat or pat.startswith(SKIP_PREFIXES):
                    continue
                concrete = pat
                for conv, name in _params(pat):
                    concrete = concrete.replace(f"<{conv}:{name}>" if conv else f"<{name}>", DUMMY.get(conv, "x"), 1)
                out.append((pat, "/" + concrete))

    walk(get_resolver().url_patterns, "")
    return out


def _params(pat):
    import re
    return [(m.group(1), m.group(2)) for m in re.finditer(r"<(?:([a-z_]+):)?([A-Za-z_]+)>", pat)]


class AnonymousAnswersTests(TestCase):
    def test_the_public_set_only_shrinks(self):
        client = Client()
        open_doors, errors = {}, []
        for pat, path in all_endpoints():
            for method in ("GET", "POST"):
                try:
                    resp = client.get(path) if method == "GET" else client.post(path, data="{}", content_type="application/json")
                    code = resp.status_code
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{method} {pat}: {type(exc).__name__}")
                    continue
                if code >= 500:
                    errors.append(f"{method} {pat}: {code}")
                elif 200 <= code < 300:
                    open_doors[f"{method} {pat}"] = code
        recorded = json.load(io.open(BASELINE, encoding="utf-8")) if os.path.exists(BASELINE) else {"_about": "", "public": {}}
        public = recorded.get("public", {})
        if os.environ.get("WRITE_ANON_BASELINE"):
            recorded = {
                "_about": "Endpoints that answer a stranger 2xx: public by design (owner rules R25 + R26, 2026-09-13). "
                          "Read by afc_auth/tests_signed_out.py: a NEW entry fails the test until it is written here "
                          "with the commit that opened it; a line whose endpoint got gated is reported stale. "
                          "Regenerate deliberately with WRITE_ANON_BASELINE=1.",
                "public": {k: {"status": v, "reason": public.get(k, {}).get("reason", "public content, decided 2026-09-13")} for k, v in sorted(open_doors.items())},
                "errors_on_anonymous_call": sorted(errors),
            }
            io.open(BASELINE, "w", encoding="utf-8", newline="\n").write(json.dumps(recorded, indent=2) + "\n")
            print(f"wrote {BASELINE}: {len(open_doors)} public answers, {len(errors)} errors")
            return
        new_doors = sorted(k for k in open_doors if k not in public)
        stale = sorted(k for k in public if k not in open_doors)
        if stale:
            print("stale (now gated, remove from tools/anonymous_answers.json):", stale)
        if errors:
            print(f"{len(errors)} endpoints answered an anonymous call with an error:", errors[:20])
        self.assertEqual(new_doors, [], f"endpoints newly answering a stranger 2xx (write them into tools/anonymous_answers.json on purpose, or gate them): {new_doors}")
