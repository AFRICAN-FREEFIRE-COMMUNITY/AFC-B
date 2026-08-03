# afc_auth/management/commands/compile_locales.py
#
# WHAT THIS IS
#   A stand-in for `manage.py compilemessages`: it turns every hand-written
#   locale/<lang>/LC_MESSAGES/*.po into the matching binary .mo that
#   django.utils.translation loads at runtime.
#
# WHY IT EXISTS (do not delete it in favour of compilemessages)
#   Django's own compilemessages shells out to GNU gettext's `msgfmt`, which is NOT
#   installed on the AFC dev machines (Windows, Git Bash) and is not part of the EB
#   deploy image either. Rather than make everyone install a C toolchain to change one
#   sentence on the consent screen, this command writes the .mo format directly. It is
#   pure Python and depends on nothing outside the standard library, which is the same
#   reasoning that made afc_auth/email_i18n.py a hand-authored catalog.
#
#   The .mo files it produces are COMMITTED, so production never has to run this. Run it
#   locally after editing a .po, then commit both the .po and the .mo together.
#
# HOW IT CONNECTS
#   - Reads   : settings.LOCALE_PATHS (afc/settings.py) -> locale/<lang>/LC_MESSAGES/*.po
#   - Feeds   : django.utils.translation, which the consent screen
#               (afc_sso/templates/afc_sso/authorize.html) renders through, once
#               afc_sso.middleware.SSOLanguageMiddleware has activated the player's language.
#   - Usage   : python manage.py compile_locales            (all locales)
#               python manage.py compile_locales --locale fr
#
# SCOPE OF THE .po SUPPORT
#   Deliberately small, because AFC's catalog is small and hand-written: comments,
#   msgctxt, msgid, msgstr, and continuation lines. Entries that are fuzzy, obsolete
#   (#~) or have an empty msgstr are skipped, exactly like msgfmt does, so an
#   untranslated entry falls back to the English msgid instead of rendering blank.
#   Plural forms (msgid_plural/msgstr[n]) are NOT supported: nothing in AFC's Django
#   templates uses them, and the command fails loudly if a .po ever introduces one
#   rather than silently dropping the string.
import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# MO magic number, little endian. Defined by the GNU gettext file format.
MO_MAGIC = 0x950412DE

# A .po line that opens a keyword, e.g. `msgid "text"`. The value may then continue on
# following lines as bare `"..."` strings, which _unescape_concat below stitches together.
_KEYWORD = re.compile(r'^(msgctxt|msgid|msgid_plural|msgstr)\s+(.*)$')
_BARE_STRING = re.compile(r'^"(.*)"\s*$')

# The C-style escapes a .po value may contain. Kept explicit rather than using
# codecs "unicode_escape", which would mangle non-ASCII (accented French/Portuguese)
# characters by decoding them as latin-1.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(raw):
    """Turn one quoted .po string literal (with the quotes still on) into its text."""
    inner = raw.strip()
    if len(inner) < 2 or not inner.startswith('"') or not inner.endswith('"'):
        raise ValueError(f"malformed .po string: {raw!r}")
    inner = inner[1:-1]
    out, i = [], 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            out.append(_ESCAPES.get(inner[i + 1], inner[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_po(path):
    """Parse a .po file into {key: translation}.

    `key` is the msgid, or "msgctxt\x04msgid" when the entry carries a context, which is
    the exact lookup key Python's gettext module builds for pgettext. Fuzzy, obsolete and
    untranslated entries are dropped so the msgid (English) shows instead.
    """
    entries = {}
    current = {"msgctxt": None, "msgid": None, "msgstr": None}
    keyword = None
    fuzzy = False

    def flush():
        nonlocal fuzzy
        msgid, msgstr = current["msgid"], current["msgstr"]
        if msgid is not None and msgstr:
            if not fuzzy:
                key = msgid if current["msgctxt"] is None else f"{current['msgctxt']}\x04{msgid}"
                entries[key] = msgstr
        current["msgctxt"] = current["msgid"] = current["msgstr"] = None
        fuzzy = False

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            flush()
            keyword = None
            continue
        if stripped.startswith("#~"):
            # Obsolete entry: skip it and everything it carries.
            keyword = None
            continue
        if stripped.startswith("#"):
            if stripped.startswith("#,") and "fuzzy" in stripped:
                fuzzy = True
            continue

        match = _KEYWORD.match(stripped)
        if match:
            keyword, value = match.group(1), match.group(2)
            if keyword == "msgid_plural":
                raise CommandError(
                    f"{path}:{lineno} uses msgid_plural; this compiler does not support "
                    "plural forms. Reword the string or extend compile_locales."
                )
            if keyword in ("msgctxt", "msgid") and current["msgstr"] is not None:
                # An entry is complete once its msgstr has been read, so the next msgctxt
                # or msgid opens a new one. This is the safety net for .po files whose
                # entries are not separated by a blank line; well-formed ones flush above.
                flush()
            current[keyword] = _unescape(value)
            continue

        bare = _BARE_STRING.match(stripped)
        if bare and keyword:
            # Continuation line: append to whatever keyword is open.
            current[keyword] = (current[keyword] or "") + _unescape(stripped)
            continue

        raise CommandError(f"{path}:{lineno} could not be parsed: {line!r}")

    flush()
    return entries


def write_mo(entries, destination):
    """Write `entries` ({msgid: msgstr}) to `destination` in GNU MO format.

    Layout, per the gettext manual: a 28-byte header, then two tables of
    (length, offset) pairs (originals first, then translations), then the string data.
    Entries are sorted by msgid because GNU tools binary-search the original table;
    Python's gettext reads it sequentially but the sort keeps the file interoperable.
    """
    keys = sorted(entries)
    originals = [k.encode("utf-8") for k in keys]
    translations = [entries[k].encode("utf-8") for k in keys]
    count = len(keys)

    # Header is 7 uint32s; each table entry is 2 uint32s.
    header_size = 7 * 4
    originals_table = header_size
    translations_table = originals_table + count * 8
    data_start = translations_table + count * 8

    offsets, blob = [], b""
    for text in originals + translations:
        offsets.append((len(text), data_start + len(blob)))
        blob += text + b"\x00"  # NUL-terminated, as msgfmt writes them

    out = struct.pack(
        "<7I",
        MO_MAGIC,
        0,                    # format revision
        count,
        originals_table,
        translations_table,
        0,                    # hash table size: 0, Python's gettext does not use one
        0,                    # hash table offset
    )
    for length, offset in offsets:
        out += struct.pack("<2I", length, offset)
    out += blob

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(out)
    return count


class Command(BaseCommand):
    help = "Compile locale/<lang>/LC_MESSAGES/*.po into .mo without needing GNU gettext."

    def add_arguments(self, parser):
        parser.add_argument(
            "--locale", action="append", dest="locales", default=None,
            help="Only compile this language code (repeatable). Default: every locale found.",
        )

    def handle(self, *args, **options):
        roots = [Path(p) for p in getattr(settings, "LOCALE_PATHS", ())]
        if not roots:
            raise CommandError("settings.LOCALE_PATHS is empty; nothing to compile.")

        wanted = set(options.get("locales") or [])
        compiled = 0
        for root in roots:
            for po in sorted(root.glob("*/LC_MESSAGES/*.po")):
                language = po.parent.parent.name
                if wanted and language not in wanted:
                    continue
                entries = parse_po(po)
                mo = po.with_suffix(".mo")
                written = write_mo(entries, mo)
                compiled += 1
                self.stdout.write(f"{language}: {written} messages -> {mo}")

        if not compiled:
            raise CommandError("No .po files found under LOCALE_PATHS.")
        self.stdout.write(self.style.SUCCESS(f"Compiled {compiled} catalog(s)."))
