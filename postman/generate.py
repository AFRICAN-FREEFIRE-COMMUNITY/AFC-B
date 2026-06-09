#!/usr/bin/env python
"""
postman/generate.py - regenerate the AFC Postman collections from the live Django
URL resolver, so the collection can NEVER drift out of sync with the code by hand.

WHAT IT DOES + HOW IT CONNECTS
------------------------------
- Walks django.urls.get_resolver() to enumerate every backend route (the exact same
  URLconf the running server dispatches on), skipping the django-admin contrib site.
- For each route it derives, with no hand-maintenance:
    * the HTTP verb(s)   -> introspected from the DRF view's http_method_names + which
                            handler methods actually exist on the wrapped view class,
    * a concrete URL     -> path/uuid/slug/id params filled with sample values,
    * the auth header     -> Bearer {{token}} by default; X-API-Key {{api_key}} for the
                            partner API; NO header for public routes (login/signup/verify/
                            reset/forgot/'not-logged-in'/'get-public-*'/news reads),
    * a placeholder JSON body for writes (POST/PUT/PATCH),
    * the view's one-line docstring (or route name) as a human label.
- Emits two Postman v2.1.0 collections NEXT TO this file (overwriting the old ones):
    AFC_Backend_API.postman_collection.json  -> every endpoint, one folder per Django app
    AFC_Smoke_GET.postman_collection.json     -> the GET-only subset, each carrying a
                                                 `status < 500` reachability test.
  These are consumed by Postman (Import) or by newman headless (see postman/README.md).
  The {{base_url}} / {{token}} / {{api_key}} variables are supplied at run time from the
  committed environment template, or from the gitignored postman/.local.env.json.

WHY a script (vs hand-editing the JSON): every time we add or change an API, you just
re-run this and commit the regenerated *.json - the collection tracks the code 1:1, and
the GET smoke is how we surface 500s (it found 7 real bugs on 2026-06-06).

RUN
---
    cd backend && .venv/Scripts/python.exe postman/generate.py

NOTE: write-request bodies are emitted as an empty `{}` placeholder - fill in the real
payload before firing a POST/PUT/PATCH deliberately. The GET smoke needs no body.
"""
import os
import re
import sys
import json

import django

# This script lives in backend/postman/; add backend/ to the path and boot Django so the
# URLconf (and every app's views) import exactly as the server sees them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
django.setup()

from django.urls import get_resolver, URLPattern, URLResolver  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Public routes get NO auth header. Matched as a substring of "<route> <name>".
# (A wrong/over-eager auth header only ever yields a 401, which is still < 500 and so
#  still "reachable" for the smoke; this list just keeps the collection honest.)
PUBLIC_HINTS = (
    "login", "signup", "verify", "resend", "reset-password", "reset_password",
    "forgot", "verify-code", "verify_code", "verify-token", "contact-us",
    "not-logged-in", "get-public-", "get-all-news", "get-news-detail",
    "connect-discord", "discord_callback",
)

# path() converter token, e.g. "<int:team_id>" or "<uidb64>" -> capture the inner name.
PARAM_RE = re.compile(r"<(?:[^:>]+:)?([^>]+)>")


def walk(resolver, prefix=""):
    """Recursively flatten the URLconf into (full_route, callback, name) triples."""
    out = []
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            out += walk(pattern, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            out.append((prefix + str(pattern.pattern), pattern.callback, pattern.name))
    return out


def methods_of(callback):
    """Allowed HTTP verbs for a DRF view = the http_method_names that have a handler.

    For an @api_view function DRF attaches a generated view class as `callback.cls`;
    only the verbs passed to @api_view get a handler method, the rest 405. A plain
    Django function view (django-admin) has no `.cls` -> returns [] and is skipped.
    """
    cls = getattr(callback, "cls", None)
    if cls is None:
        return []
    skip = {"options", "head", "trace"}
    return [m.upper() for m in getattr(cls, "http_method_names", [])
            if m not in skip and hasattr(cls, m)]


def fill_params(route):
    """Turn a Django route into a concrete sample URL path (params -> sample values)."""
    def sub(match):
        token = match.group(0).lower()      # e.g. "<uuid:ghost_team_id>"
        name = match.group(1).lower()       # e.g. "ghost_team_id"
        if "uuid" in token:
            return "11111111-1111-1111-1111-111111111111"
        if "uidb64" in token:
            return "MQ"
        if "slug" in token or name.endswith("slug") or "name" in name:
            return "sample-slug"
        if "token" in name:
            return "sampletoken"
        if "int" in token or name.endswith("id") or name == "pk":
            return "1"
        return "sample"

    path = PARAM_RE.sub(sub, route)
    # re_path() named groups: (?P<name>...) -> sample value.
    path = re.sub(r"\(\?P<(\w+)>[^)]*\)",
                  lambda m: "1" if m.group(1).lower().endswith("id") else "sample", path)
    # Strip the regex anchors/escapes re_path leaves in str(pattern); keep trailing slash.
    path = path.lstrip("^").rstrip("$").replace("\\/", "/").replace("\\.", ".").replace("\\", "")
    return path.lstrip("/")


def auth_header(route, name):
    key = (route + " " + (name or "")).lower()
    if "partner" in key or route.startswith("api/v1/partner"):
        return [{"key": "X-API-Key", "value": "{{api_key}}"}]
    if any(hint in key for hint in PUBLIC_HINTS):
        return []
    return [{"key": "Authorization", "value": "Bearer {{token}}"}]


def describe(callback, name):
    doc = (getattr(callback, "__doc__", None)
           or getattr(getattr(callback, "cls", None), "__doc__", None)
           or "")
    line = doc.strip().split("\n")[0].strip()
    return (line or (name or ""))[:120]


def make_request(route, method, callback, name):
    url_path = fill_params(route)
    segments = [s for s in url_path.split("/") if s]
    header = auth_header(route, name)
    request = {
        "method": method,
        "header": list(header),
        "url": {"raw": "{{base_url}}/" + url_path, "host": ["{{base_url}}"], "path": segments},
    }
    if method in ("POST", "PUT", "PATCH"):
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {"mode": "raw", "raw": "{}", "options": {"raw": {"language": "json"}}}
    desc = describe(callback, name)
    return {
        # Spaced hyphen, never an em dash (AFC hard rule), even in tooling artifacts.
        "name": f"{method} /{url_path}" + (f" - {desc}" if desc else ""),
        "request": request,
        "event": [{
            "listen": "test",
            "script": {"type": "text/javascript", "exec": [
                "pm.test('reachable (status < 500)', () => pm.expect(pm.response.code).to.be.below(500));"
            ]},
        }],
    }


def groups_of(callback):
    """Two-level folder key for a view: (app_label, feature_module).

    Folders are NESTED so the collection mirrors the codebase instead of dumping every
    endpoint of a big app into one flat list. Derived from the view function's module:
      __module__ "afc_shop.fulfilment" -> app "shop", feature "fulfilment"
      __module__ "afc_shop.views"      -> app "shop", feature "views"
      __module__ "afc_auth"            -> app "auth",  feature "views"  (no submodule)
    The "afc_" prefix is stripped from the app label for a cleaner top folder name; the
    feature is the module FILE the view lives in (e.g. views / fulfilment / vendors /
    connect / paystack_payout / whatsapp_webhook / stripe_checkout / mintroute). Deeper
    dotted modules collapse to their first sub-component so the tree stays 2 levels.
    """
    mod = (getattr(callback, "__module__", "") or "misc")
    parts = mod.split(".")
    app = parts[0] or "misc"
    app_label = app[4:] if app.startswith("afc_") else app   # afc_shop -> shop
    feature = parts[1] if len(parts) > 1 else "views"          # the module file = the feature
    return app_label, feature


def _folder(name, items):
    """A Postman folder node (sorted children) used at both nesting levels."""
    return {"name": name, "item": items}


def collection(title, data):
    """Build a v2.1.0 collection from a nested {app: {feature: [items]}} dict.

    Top-level folders = app (sorted); each holds sub-folders = feature module (sorted),
    each holding its requests. Apps with a single feature still nest (one sub-folder) so
    the structure is uniform and predictable for anyone browsing the collection.
    """
    top = []
    for app in sorted(data):
        subfolders = [_folder(feat, data[app][feat]) for feat in sorted(data[app])]
        top.append(_folder(app, subfolders))
    return {
        "info": {
            "name": title,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000"},
            {"key": "token", "value": ""},
            {"key": "api_key", "value": ""},
        ],
        "item": top,
    }


def build():
    routes = walk(get_resolver())
    # Nested grouping: data[app_label][feature_module] -> [request items].
    full, smoke = {}, {}
    n_full = n_smoke = 0
    for route, callback, name in routes:
        app_label, feature = groups_of(callback)
        if (getattr(callback, "__module__", "") or "").startswith("django"):
            continue   # skip the django-admin contrib site
        for method in methods_of(callback):
            item = make_request(route, method, callback, name)
            full.setdefault(app_label, {}).setdefault(feature, []).append(item)
            n_full += 1
            if method == "GET":
                smoke.setdefault(app_label, {}).setdefault(feature, []).append(item)
                n_smoke += 1

    with open(os.path.join(HERE, "AFC_Backend_API.postman_collection.json"), "w", encoding="utf-8") as f:
        json.dump(collection("AFC Backend API", full), f, indent=2)
    with open(os.path.join(HERE, "AFC_Smoke_GET.postman_collection.json"), "w", encoding="utf-8") as f:
        json.dump(collection("AFC Backend API - GET smoke", smoke), f, indent=2)

    n_full_folders = sum(len(feats) for feats in full.values())
    n_smoke_folders = sum(len(feats) for feats in smoke.values())
    print(f"Wrote full collection: {n_full} requests across {len(full)} apps / {n_full_folders} feature folders")
    print(f"Wrote GET smoke:       {n_smoke} requests across {len(smoke)} apps / {n_smoke_folders} feature folders")


if __name__ == "__main__":
    build()
