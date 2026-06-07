#!/usr/bin/env python
"""
postman/merge_into_dev.py - keep the hand-curated DEV collection (the one the team
"drives into" in Postman) up to date with the code, WITHOUT flattening its folder
structure into the auto layout.

HOW IT WORKS
------------
- AUTO  = backend/postman/AFC_Backend_API.postman_collection.json  (regenerated 1:1 from
          the Django URL resolver by generate.py - the source of truth for "what exists").
- DEV   = WEBSITE/AFC.postman_collection.json                       (the team's collection,
          with its own folders + environment usage).
- For every endpoint that exists in AUTO but is MISSING from DEV (compared by
  "METHOD /path/", base-url-agnostic), we APPEND it to the DEV folder that already holds
  endpoints from the SAME app/path-prefix (so new shop routes land in the shop folder,
  etc.). If no existing folder matches that prefix, we create one folder named after the
  prefix (only as a last resort). Existing DEV requests are never modified or reordered.
- A timestamped backup of DEV is written before saving (AFC.postman_collection.bak.json).

RUN:  cd backend && .venv/Scripts/python.exe postman/merge_into_dev.py
"""
import os
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = os.path.join(HERE, "AFC_Backend_API.postman_collection.json")
DEV = os.path.abspath(os.path.join(HERE, "..", "..", "AFC.postman_collection.json"))


def raw_url(req):
    """The collection-relative path of a request, e.g. '/shop/view-active-products/'.
    Strips any {{base_url}} / host prefix so AUTO and DEV compare apples to apples."""
    u = (req.get("request", {}) or {}).get("url", {})
    raw = u.get("raw", "") if isinstance(u, dict) else (u or "")
    raw = raw.split("?")[0]
    # drop everything up to and including the host/base-url variable
    for marker in ("{{base_url}}", "{{url}}", "{{baseUrl}}"):
        if marker in raw:
            raw = raw.split(marker, 1)[1]
            break
    else:
        # absolute http(s) url -> keep just the path
        if raw.startswith("http"):
            raw = "/" + raw.split("/", 3)[-1] if raw.count("/") >= 3 else raw
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") + "/"


def method(req):
    return (req.get("request", {}) or {}).get("method", "GET").upper()


def key(req):
    return f"{method(req)} {raw_url(req)}"


def walk(items):
    """Yield (request_item, parent_list) for every leaf request, recursing folders."""
    for it in items:
        if "item" in it:
            yield from walk(it["item"])
        elif "request" in it:
            yield it


def prefix(path):
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else "misc"


auto = json.load(open(AUTO, encoding="utf-8"))
dev = json.load(open(DEV, encoding="utf-8"))

dev_keys = {key(r) for r in walk(dev["item"])}

# Map a path-prefix -> the DEV folder (its "item" list) that already holds that prefix,
# choosing the folder that holds the most endpoints of that prefix.
from collections import Counter, defaultdict

folder_prefix_counts = defaultdict(Counter)


def index_folders(items, path_stack):
    for it in items:
        if "item" in it:
            index_folders(it["item"], path_stack + [it])
        elif "request" in it and path_stack:
            folder_prefix_counts[id(path_stack[-1])][prefix(raw_url(it))] += 1


index_folders(dev["item"], [])

# folder object lookup by id
folder_by_id = {}


def collect_folders(items):
    for it in items:
        if "item" in it:
            folder_by_id[id(it)] = it
            collect_folders(it["item"])


collect_folders(dev["item"])

best_folder_for_prefix = {}
for fid, counts in folder_prefix_counts.items():
    for pfx, c in counts.items():
        cur = best_folder_for_prefix.get(pfx)
        if cur is None or c > cur[1]:
            best_folder_for_prefix[pfx] = (folder_by_id[fid], c)

added = 0
created_folders = {}
for r in walk(auto["item"]):
    if key(r) in dev_keys:
        continue
    pfx = prefix(raw_url(r))
    target = best_folder_for_prefix.get(pfx)
    if target:
        target[0]["item"].append(r)
    else:
        # last-resort: a folder named after the prefix (kept minimal, not an "AUTO" dump)
        if pfx not in created_folders:
            folder = {"name": pfx, "item": []}
            dev["item"].append(folder)
            created_folders[pfx] = folder
        created_folders[pfx]["item"].append(r)
    dev_keys.add(key(r))
    added += 1

if added:
    shutil.copyfile(DEV, DEV.replace(".json", ".bak.json"))
    json.dump(dev, open(DEV, "w", encoding="utf-8"), indent=2)

print(f"merged {added} missing endpoint(s) into the dev collection")
if created_folders:
    print("new folders created (no existing match):", ", ".join(created_folders))
print("total endpoints now in dev collection:", len(list(walk(dev['item']))))
