"""
tools/kol_sheet_to_json.py <master-sheet.xlsx> <out.json>
tools/kol_sheet_to_json.py --contacts <master-sheet.xlsx> <creators.json> <out.json>

Turns Garena's private KOL master sheet into the file `manage.py kol_import_creators` loads (AFC inbox #633 / #634).

Reads ONLY the "KOL List" tab and ONLY five columns: Creator Name, TikTok, Country, Status, TT ID. Game UIDs are
never read. The handle comes from the TikTok link (tiktok.com/@<handle>), or the cell itself when it is a bare handle.

--contacts (owner, 27 Nov 2026, inbox #646: "send an email to the KOLs which we already have their mails") writes the
file `manage.py kol_import_contacts` loads instead: [{tiktok_handle, email}] for the creators of <creators.json> (the
first mode's output) that the sheet has an email for. Two places: the KOL List's own "Email" column, then the "KOL
Emails" tab (KOL, Email Address), matched to ONE creator by name or handle (letters and digits only, case ignored); a
KOL name matching nobody, or more than one creator, is counted and left out. Prints counts only, never an address.

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


EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def contacts(src, creators_json, dst):
    with open(creators_json, encoding="utf-8") as handle:
        creators = json.load(handle)
    known = {row["tiktok_handle"] for row in creators}
    by_key = {}
    for row in creators:
        for text in (row["name"], row["tiktok_handle"]):
            by_key.setdefault(_key(text), set()).add(row["tiktok_handle"])
    book = load_workbook(src, read_only=True, data_only=True)
    found = {}

    rows = book[TAB].iter_rows(values_only=True)
    header = [str(cell or "").strip() for cell in next(rows)]
    if "Email" in header and "TikTok" in header:
        at_email, at_tiktok = header.index("Email"), header.index("TikTok")
        for row in rows:
            email = str((row[at_email] if at_email < len(row) else "") or "").strip()
            handle = handle_from(row[at_tiktok] if at_tiktok < len(row) else None)
            if handle in known and EMAIL.match(email):
                found[handle] = email

    unmatched = ambiguous = 0
    if "KOL Emails" in book.sheetnames:
        rows = book["KOL Emails"].iter_rows(values_only=True)
        header = []
        for row in rows:                               # the tab starts with blank rows above its header
            header = [str(cell or "").strip() for cell in row]
            if "KOL" in header and "Email Address" in header:
                break
        else:
            sys.exit("KOL Emails: header row (KOL, Email Address) not found")
        at_name, at_email = header.index("KOL"), header.index("Email Address")
        for row in rows:
            email = str((row[at_email] if at_email < len(row) else "") or "").strip()
            if not EMAIL.match(email):
                continue
            hits = by_key.get(_key(row[at_name] if at_name < len(row) else ""), set())
            if len(hits) == 1:
                found.setdefault(next(iter(hits)), email)
            elif hits:
                ambiguous += 1
            else:
                unmatched += 1

    out = [{"tiktok_handle": handle, "email": email} for handle, email in sorted(found.items())]
    with open(dst, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=1)
    print(f"{len(out)} of {len(creators)} creators have an email; KOL Emails rows matching nobody: {unmatched}, "
          f"more than one creator: {ambiguous}")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--contacts":
        contacts(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        sys.exit(__doc__)
