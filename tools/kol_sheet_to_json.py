"""
tools/kol_sheet_to_json.py <master-sheet.xlsx> <out.json>

Turns Garena's private KOL master sheet into the file `manage.py kol_import_creators` loads (AFC inbox #633 / #634).

Reads ONLY the "KOL List" tab and ONLY five columns: Creator Name, TikTok, Country, Status, TT ID. The sheet also
holds emails and game UIDs; they are never read, so they can never reach the JSON, the server or a log. The handle
comes from the TikTok link (tiktok.com/@<handle>), or the cell itself when it is a bare handle.

Run it on a workstation with `python -I` and write the JSON next to the sheet, in the git-ignored private folder
(projectsmanagement/PERSONAL/AFC/_private/kol-master-sheet/). Needs openpyxl (in requirements*.txt).
"""
import json
import re
import sys

from openpyxl import load_workbook

TAB = "KOL List"
WANTED = {"Creator Name": "name", "TikTok": "tiktok", "Country": "country", "Status": "status", "TT ID": "tiktok_id"}
HANDLE_IN_LINK = re.compile(r"tiktok\.com/@([A-Za-z0-9._]+)", re.IGNORECASE)
BARE_HANDLE = re.compile(r"^@?([A-Za-z0-9._]{2,})$")


def handle_from(cell):
    text = str(cell or "").strip()
    found = HANDLE_IN_LINK.search(text) or BARE_HANDLE.match(text)
    return found.group(1).lower() if found else ""


def main(src, dst):
    sheet = load_workbook(src, read_only=True, data_only=True)[TAB]
    rows = sheet.iter_rows(values_only=True)
    header = [str(cell or "").strip() for cell in next(rows)]
    # The FIRST column carrying each wanted name (the sheet repeats "UID"; none of the wanted names repeat).
    where = {key: header.index(name) for name, key in WANTED.items() if name in header}
    missing = [name for name, key in WANTED.items() if key not in where]
    if missing:
        sys.exit(f"{TAB}: columns not found: {', '.join(missing)}")

    out, seen, skipped = [], set(), 0
    for row in rows:
        def cell(key):
            index = where[key]
            return row[index] if index < len(row) else None

        name = str(cell("name") or "").strip()
        handle = handle_from(cell("tiktok"))
        if not name or not handle or handle in seen:
            skipped += 1
            continue
        seen.add(handle)
        tiktok_id = cell("tiktok_id")
        out.append({
            "name": name,
            "tiktok_handle": handle,
            "country": str(cell("country") or "").strip(),
            "status": str(cell("status") or "").strip(),
            "tiktok_id": "" if tiktok_id is None else str(tiktok_id).strip().removesuffix(".0"),
        })
    with open(dst, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=1)
    print(f"{len(out)} creators written, {skipped} rows skipped (empty, no handle or repeated)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
