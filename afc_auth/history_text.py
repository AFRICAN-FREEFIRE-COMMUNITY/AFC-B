# afc_auth/history_text.py
# ──────────────────────────────────────────────────────────────────────────────────────────────
# PLAIN ENGLISH FOR THE ADMIN HISTORY.
#
# WHY THIS EXISTS. The owner, 2026-09-03, looking at the dashboard's Recent Admin Activities:
#
#     "cant the action be put into plainer english? edited roles for user ARDENT from what to
#      what? of event id 332 changes to, what does that mean? or this:
#      { "event_id": 333, "changes": [ "event_name: '🔥 ARE ESPORTS ... what does that
#      mean, we need plainer english please"
#
# He is quoting the screen exactly. edit_event writes its AdminHistory row as json.dumps(...), so
# the table was printing a JSON document at a human, escape sequences and all. Measured on the
# clone: 221 of 1,075 rows are that JSON blob, and 119 of those 221 record an event edit that
# CHANGED NOTHING, which is the single most common thing the log has to say.
#
# WHY AT READ TIME. Rewriting the 55 write sites would only improve rows written from now on, and
# the owner is looking at rows written since June. Everything here therefore parses what is
# ALREADY in the table. The one thing read-time work cannot do is recover a detail nobody wrote
# down (which roles a user held before an edit), so that ONE write site was fixed as well, in
# afc_auth/views.py -> edit_user_roles.
#
# HOW IT CONNECTS.
#   Written by  : nothing. This module never writes.
#   Reads       : the AdminHistory.action slug and AdminHistory.description text, plus an
#                 optional {event_id: event_name} map the caller bulk-fetches (see event_names()).
#   Consumed by : afc_auth/views_dashboard.py   -> admin_dashboard_stats ("activity.recent")
#                                                 and _detail_activity (the breakdown ledger)
#                 afc_auth/views.py             -> get_admin_activities, get_admin_history,
#                                                 get_audit_log (slug fallback only)
#                 frontend app/(a)/a/dashboard/page.tsx renders {summary, details}: one sentence
#                 per row, with the full change list behind a click.
#
# THE RULE THIS FILE FOLLOWS. Never invent. Where the stored row does not say something, the text
# says what IS known and stops. An unparseable description falls back to itself rather than to a
# guess, so a shape nobody anticipated degrades to today's behaviour instead of to fiction.
# ──────────────────────────────────────────────────────────────────────────────────────────────
import json
import re

# ── 1. action slugs ────────────────────────────────────────────────────────────────────────────
# The stored slug, e.g. "edit_event", is what the breakdown table groups by. Grouping must stay on
# the raw slug (it is the identity); only the LABEL is translated. Anything not listed falls
# through to _prettify_slug, so a new action added tomorrow reads acceptably with no edit here.
ACTION_LABELS = {
    "create_event": "Created an event",
    "edit_event": "Edited an event",
    "delete_event": "Deleted an event",
    "duplicate_event": "Duplicated an event",
    "cancel_event": "Cancelled an event",
    "reopen_event": "Reopened an event",
    "edited_user_roles": "Changed a user's roles",
    "added_nominee": "Added an award nominee",
    "added_category": "Added an award category",
    "created_news": "Published a news post",
    "edited_news": "Edited a news post",
    "deleted_news": "Deleted a news post",
    "banned_team": "Banned a team",
    "unbanned_team": "Unbanned a team",
    "banned_player": "Banned a player",
    "unbanned_player": "Unbanned a player",
    "suspended_user": "Suspended a user",
    "activated_user": "Reactivated a user",
    "deleted_role": "Deleted a role",
    "broadcast_announcement": "Sent an announcement",
    "broadcast_to_group": "Sent a group broadcast",
    "broadcast_match_room_details": "Sent match room details",
}


def _prettify_slug(slug):
    """"edited_something_new" -> "Edited something new". The fallback label, never a guess about
    what the action did beyond the words already in the slug itself."""
    if not slug:
        return "Did something"
    words = str(slug).replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Did something"


def humanize_action(slug):
    """One label for an action slug. Used by the breakdown ledger's "By action" table, and as the
    last-resort summary when a row has no description at all."""
    return ACTION_LABELS.get(slug, _prettify_slug(slug))


# ── 2. event field names ───────────────────────────────────────────────────────────────────────
# The change lines the event editor writes are "field: 'old' -> 'new'" using the raw column name.
# These are the columns that actually appear in the log (measured, not imagined), each mapped to
# the words a person would use. Everything else prettifies its own column name.
FIELD_LABELS = {
    "event_name": "name",
    "event_status": "status",
    "event_type": "event type",
    "event_mode": "game mode",
    "competition_type": "competition type",
    "participant_type": "participant type",
    "max_teams_or_players": "capacity",
    "number_of_stages": "number of stages",
    "start_date": "start date",
    "end_date": "end date",
    "event_start_time": "start time",
    "event_end_time": "end time",
    "registration_open_date": "registration opening date",
    "registration_end_date": "registration closing date",
    "registration_start_time": "registration opening time",
    "registration_end_time": "registration closing time",
    "tournament_tier": "tier",
    "prize_pool": "prize pool",
    "currency": "currency",
    "timezone": "timezone",
    "description": "description",
    "rules": "rules",
    "waitlist_mode": "waitlist mode",
    "check_in_required": "check-in requirement",
    "require_discord": "Discord requirement",
    "slug": "web address",
}

# Columns whose values are yes/no. "is_draft: 'True' -> 'False'" is meaningless as a sentence, so
# each of these gets its own phrasing for the true and the false direction.
BOOLEAN_PHRASES = {
    "is_draft": ("moved it back to draft", "published it"),
    "is_public": ("made it public", "made it private"),
    "is_sponsored": ("marked it as sponsored", "removed its sponsored flag"),
    "check_in_required": ("turned check-in on", "turned check-in off"),
    "require_discord": ("made Discord required", "made Discord optional"),
    "results_published": ("published the results", "hid the results"),
}


def _clean(value):
    """Strip the quotes the change line wraps values in, and normalise the empty cases so the
    sentence reads "set it" / "cleared it" rather than printing None or an empty pair of quotes."""
    if value is None:
        return ""
    v = str(value).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    if v.lower() in ("none", "null", ""):
        return ""
    return v


def _is_true(value):
    return _clean(value).lower() in ("true", "1", "yes")


def _tidy_value(value):
    """Values as a person reads them: tier_1 -> tier 1, upcoming -> upcoming, 2026-07-03 as is.
    Dates are LEFT in ISO on purpose. They are unambiguous, and the viewer's own timezone applies
    to timestamps, not to the calendar date an organizer typed into the form."""
    v = _clean(value)
    if re.fullmatch(r"[a-z]+_[a-z0-9_]+", v):        # tier_1, point_rush, clash_squad
        return v.replace("_", " ")
    return v


def _field_label(field):
    f = str(field).strip()
    return FIELD_LABELS.get(f, f.replace("_", " ").strip())


# ── 3. one change line -> one clause ───────────────────────────────────────────────────────────
# Shapes actually present in the table (all four measured on the clone, none invented):
#   "event_name: 'A' -> 'B'"                 a plain field edit
#   "Stage 326 name: 'GROUP STAGE' -> 'X'"   a stage or group field edit
#   "Group 652 maps changed"                 a bare statement with no old/new pair
#   "Stages added: [328, 327]"               a list of ids added or removed
_ARROW = "→"                 # the character diff_dict writes between old and new
_PAIR = re.compile(r"^(?P<field>[^:]+):\s*(?P<old>.*?)\s*" + _ARROW + r"\s*(?P<new>.*)$")
_LIST = re.compile(r"^(?P<what>[A-Za-z][A-Za-z ]*?)\s+(?P<verb>added|removed):\s*\[(?P<ids>.*)\]$")
# The same idea nested under a stage or a group: "Stage 374: groups added [846]". Written by
# diff_stages rather than diff_list, which is why it needs its own pattern.
_SCOPED_LIST = re.compile(
    r"^(?P<owner>(?:Stage|Group)\s+\d+):\s*(?P<what>[a-z ]+?)\s+(?P<verb>added|removed):?\s*"
    r"\[(?P<ids>.*)\]$"
)


def humanize_change(line):
    """Turn ONE stored change line into one clause of a sentence.

    Returns the clause without a leading capital, because clauses are joined into a sentence by
    describe_history() below. An unrecognised line is returned as-is: today's behaviour for that
    line, never a fabricated reading of it.
    """
    if not line:
        return ""
    text = str(line).strip()

    # "Stage 374: groups added [846]" keeps the stage it happened to, because "added 1 group" on
    # its own does not tell an organizer which part of the bracket moved.
    m = _SCOPED_LIST.match(text)
    if m:
        ids = [i.strip().strip("'\"") for i in m.group("ids").split(",") if i.strip()]
        what = m.group("what").strip()
        noun = what if len(ids) != 1 else re.sub(r"s$", "", what)
        joiner = "to" if m.group("verb") == "added" else "from"
        return f"{m.group('verb')} {len(ids)} {noun} {joiner} {m.group('owner')}"

    # "Stages added: [328, 327]" / "Stream channels added: ['https://...']"
    m = _LIST.match(text)
    if m:
        what = m.group("what").strip().lower()
        raw_ids = [i.strip().strip("'\"") for i in m.group("ids").split(",") if i.strip()]
        count = len(raw_ids)
        noun = what if count != 1 else re.sub(r"s$", "", what)
        return f"{m.group('verb')} {count} {noun}" if count else f"{m.group('verb')} {what}"

    m = _PAIR.match(text)
    if not m:
        # "Group 652 maps changed" and anything else with no old/new pair. Lower-cased so it sits
        # inside the sentence, with any leading id phrase kept because it identifies the thing.
        return text[:1].lower() + text[1:] if text else ""

    field_raw = m.group("field").strip()
    old, new = m.group("old"), m.group("new")

    # A stage or group edit carries its own prefix: "Stage 326 name", "Group 652 match_count".
    prefix = ""
    scoped = re.match(r"^(?P<owner>(?:Stage|Group)\s+\d+)\s+(?P<field>.+)$", field_raw)
    if scoped:
        prefix = scoped.group("owner") + " "
        field_raw = scoped.group("field").strip()

    # Yes/no columns read as an action, never as True and False.
    if not prefix and field_raw in BOOLEAN_PHRASES:
        to_true, to_false = BOOLEAN_PHRASES[field_raw]
        return to_true if _is_true(new) else to_false

    label = _field_label(field_raw)
    old_v, new_v = _tidy_value(old), _tidy_value(new)

    # The name is the one field where "renamed it to X" beats "name changed from X to Y", because
    # the old name is already the row above it in the log.
    if not prefix and field_raw == "event_name" and new_v:
        return f"renamed it to \"{new_v}\""

    if not old_v and new_v:
        return f"set {prefix}{label} to {new_v}"
    if old_v and not new_v:
        return f"cleared {prefix}{label}"
    if not old_v and not new_v:
        return f"changed {prefix}{label}"
    return f"changed {prefix}{label} from {old_v} to {new_v}"


# ── 4. one history row -> one sentence plus its details ────────────────────────────────────────
_MAX_CLAUSES = 2   # in the one-line summary. The rest are counted, and all of them ride in details.


def describe_history(action, description, event_names=None):
    """Render one AdminHistory row for a human.

    action       the stored slug, e.g. "edit_event"
    description  the stored description text, which may be a JSON document
    event_names  optional {event_id: event_name}, so the sentence can say WHICH event rather than
                 a bare id. Callers bulk-fetch it once for a page of rows: see event_names().

    Returns {"summary": str, "details": [str, ...]}. `details` is empty for rows that were already
    a sentence, and holds every individual change for rows that were a JSON document, so the UI
    can show one line and reveal the rest on click.
    """
    text = (description or "").strip()
    if not text:
        return {"summary": humanize_action(action), "details": []}

    # Rows that are already English (most of them) pass straight through. That is deliberate:
    # "Created event LEGACY QUALIFIERS (ID: 344)" needs nothing done to it.
    if not text.startswith("{"):
        return {"summary": text, "details": []}

    try:
        blob = json.loads(text)
    except (ValueError, TypeError):
        # A JSON-looking description that will not parse: show it rather than hide it, but do not
        # pretend to have read it.
        return {"summary": text, "details": []}

    if not isinstance(blob, dict):
        return {"summary": text, "details": []}

    event_id = blob.get("event_id")
    changes = blob.get("changes") or []
    if not isinstance(changes, list):
        changes = [str(changes)]

    # WHICH event. The name when the caller supplied one, the id when it did not, and both when
    # the name is known, because the id is what every other admin screen is keyed by.
    name = (event_names or {}).get(event_id)
    if name and event_id:
        target = f"{name} (event {event_id})"
    elif name:
        target = name
    elif event_id:
        target = f"event {event_id}"
    else:
        target = "an event"

    clauses = [c for c in (humanize_change(line) for line in changes) if c]

    # The most common single thing in the log: an edit that saved without changing anything. It
    # said `"changes": []` on screen, which is exactly the complaint.
    if not clauses:
        return {"summary": f"Saved {target} without changing anything", "details": []}

    shown = clauses[:_MAX_CLAUSES]
    rest = len(clauses) - len(shown)
    body = ", ".join(shown)
    if rest == 1:
        body += ", and 1 more change"
    elif rest > 1:
        body += f", and {rest} more changes"
    # `details` is the REMAINDER the sentence could not carry, expressed as the whole list. When
    # nothing was truncated it is empty, because a UI that prints the same two clauses twice, once
    # as a sentence and once as a list, is noise. Verified on the real rows during the walk.
    return {"summary": f"Edited {target}: {body}", "details": clauses if rest else []}


# ── 5. resolving event names in bulk ───────────────────────────────────────────────────────────
def event_names(descriptions):
    """{event_id: event_name} for every event id mentioned in these descriptions, in ONE query.

    Called by the read endpoints before they render a page of rows, so a 10-row table costs one
    extra query rather than ten. Import is local because afc_auth must not import
    afc_tournament_and_scrims at module load (that app imports afc_auth back).
    """
    ids = set()
    for text in descriptions or []:
        t = (text or "").strip()
        if not t.startswith("{"):
            continue
        try:
            blob = json.loads(t)
        except (ValueError, TypeError):
            continue
        if isinstance(blob, dict) and isinstance(blob.get("event_id"), int):
            ids.add(blob["event_id"])
    if not ids:
        return {}
    from afc_tournament_and_scrims.models import Event
    return dict(
        Event.objects.filter(event_id__in=ids).values_list("event_id", "event_name")
    )
