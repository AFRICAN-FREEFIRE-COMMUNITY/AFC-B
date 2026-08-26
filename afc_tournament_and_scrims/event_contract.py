"""ONE CONTRACT FOR THE EVENT OBJECT (owner 2026-08-26).

WHY THIS FILE EXISTS
    `Event` carries 87 fields. Before this module, six functions listed those fields BY HAND:
    create_event (58 assignments), edit_event (41 assignments behind 45 separate
    `if "x" in request.data` guards), duplicate_event (an Event.objects.create call with 79 keyword
    arguments typed out), and the three readers get_event_details, get_event_details_not_logged_in
    and get_event_details_for_admin.

    Adding ONE field therefore meant about a dozen edits. The site count was wrong twice while
    `required_connections` was being built, and a single falsy check reached production through the
    create wizard, the edit form and the readers at once, blocking event creation entirely. Worse,
    duplicate_event's hand-written list fails SILENTLY: a field nobody remembered to add there just
    disappears from every duplicated event, with no error anywhere.

    See the hard rule "One contract per domain object" in WEBSITE/CLAUDE.md.

WHAT IT DOES
    Declares each exposed field ONCE, with the role allowed to READ it and the role allowed to
    WRITE it, then serves both directions from that single table:

        serialize_event(event, viewer=..., request=...)  -> dict, filtered by the viewer's role
        apply_event_writes(event, data, actor=...)       -> list of field names actually changed

WHO CALLS IT
    afc_tournament_and_scrims/views.py: get_event_details, get_event_details_not_logged_in,
    get_event_details_for_admin, create_event, edit_event, duplicate_event.

THE LADDER IS NOT A NEW IDEA, IT IS WHAT THE CODE ALREADY DID
    Counted field by field on 2026-08-26: the public reader touched 53 Event fields, the signed-in
    reader 64, and the admin endpoint 32. BOTH the public set AND the admin set were EXACT subsets
    of the signed-in set, with nothing escaping the ladder in either direction. Declaring
    PUBLIC < PLAYER < ORGANIZER < ADMIN reproduces that; it does not change it.

WHY NOT A DRF ModelSerializer
    Two reasons, both concrete. The events app has ZERO serializer classes, so a serializer layer
    is a paradigm change across every event endpoint. And get_event_details_for_admin emits the
    registration_open_date COLUMN under the key "registration_start_date" (views.py:11247), which a
    serializer derived from the model cannot produce. A declared contract with an explicit output
    key per field handles that as a one-line `source=`.
"""
from dataclasses import dataclass
from typing import Callable, Optional

# ── the ladder ────────────────────────────────────────────────────────────────────────────────
# Ordered least to most. A field readable at PUBLIC is readable by everyone above it.
PUBLIC = "public"        # anybody, signed in or not
PLAYER = "player"        # any signed-in user
ORGANIZER = "organizer"  # can act on this event's owning org, or is an AFC event admin, or created it
ADMIN = "admin"          # AFC staff, plus the granular head_admin / event_admin roles
NOBODY = "nobody"        # never exposed, or never writable through the API

_RANK = {PUBLIC: 0, PLAYER: 1, ORGANIZER: 2, ADMIN: 3}


def satisfies(actual, required):
    """True when a viewer or actor at `actual` clears the bar `required`.

    NOBODY is deliberately NOT a rung: it means "no one, ever", so it is never satisfied and never
    satisfies anything. Keeping it out of _RANK is what makes that fall out for free rather than
    needing a special case at every call site.
    """
    if required == NOBODY or actual == NOBODY:
        return False
    return _RANK.get(actual, -1) >= _RANK.get(required, 99)


def role_of(user, event, perm="can_edit_events"):
    """Resolve a viewer's rung for THIS event.

    Deliberately reuses the authority helpers that already exist rather than adding yet another
    copy of them (`_is_event_admin` alone is currently copy-pasted into six modules):

      - views._is_event_admin    AFC staff plus the granular head_admin / event_admin roles
      - views._is_event_creator  the creator bypass, which matters because a native event has no
                                 owning organization and org_can_event is admin-only for those
      - afc_organizers.permissions.org_can_event
                                 the primary org plus any ACCEPTED co-owning org

    Imported INSIDE the function on purpose: views.py imports this module, so a module-level import
    back into views.py would be circular.
    """
    if user is None or not getattr(user, "is_authenticated", True):
        return PUBLIC

    from afc_organizers.permissions import org_can_event

    from .views import _is_event_admin, _is_event_creator

    if _is_event_admin(user):
        return ADMIN
    if _is_event_creator(user, event) or org_can_event(user, perm, event):
        return ORGANIZER
    return PLAYER


# ── one field, declared once ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Field:
    """A single event field: what it is called on the wire, who may read it, who may write it.

    name    the OUTPUT key, which is what the frontend reads. Usually the model attribute too.
    read    the lowest rung allowed to see it. NOBODY means it is never serialised.
    write   the lowest rung allowed to set it. NOBODY (the default) means it is never writable
            through the API, which is the right default: a field has to be opted IN to writing.
    source  the model attribute, when it differs from `name`. Not hypothetical: the admin endpoint
            emits registration_open_date under the key registration_start_date.
    get     (event, ctx) -> value, for a computed key such as event_banner_url, which needs the
            request to build an absolute URI. ctx carries {"request", "role", "extra"}.
    clean   (raw) -> value, run on WRITE. Raises ValueError to reject. NEVER gate on truthiness in
            here: a legitimately empty list has to survive (the 2026-08-26 outage).
    default a zero-argument callable used by the writer on CREATE when the request omits the key.
    """

    name: str
    read: str
    write: str = NOBODY
    source: Optional[str] = None
    get: Optional[Callable] = None
    clean: Optional[Callable] = None
    default: Optional[Callable] = None

    @property
    def attr(self):
        """The model attribute this field reads from and writes to."""
        return self.source or self.name


# ── the field table ───────────────────────────────────────────────────────────────────────────
# Filled in as the readers and writers are converted. Order here IS the output order of every
# payload, so rows stay grouped the way the existing readers grouped them.
EVENT_FIELDS = []


def serialize_event(event, *, viewer=None, request=None, role=None, table=None,
                    fields=None, extra=None):
    """Turn an event into the dict a viewer at `role` is allowed to see.

    role   pass it directly when the caller already knows it, which the three readers do because
           each serves exactly one audience. Otherwise it is resolved from `viewer` via role_of.
    table  the field table. Defaults to EVENT_FIELDS; the unit tests pass a small table so they
           exercise the machinery rather than the real 64-row declaration.
    fields restrict the output to these names. The order of the OUTPUT stays declaration order,
           not the order given, because the golden files compare exactly.
    extra  values the caller already computed and does not want computed twice (published sponsors,
           the capacity snapshot, the effective status), handed to each `get` through ctx.
    """
    if role is None:
        role = role_of(viewer, event) if viewer is not None else PUBLIC
    rows = EVENT_FIELDS if table is None else table
    wanted = None if fields is None else set(fields)
    ctx = {"request": request, "role": role, "extra": extra or {}}

    out = {}
    for f in rows:
        if wanted is not None and f.name not in wanted:
            continue
        if not satisfies(role, f.read):
            continue
        out[f.name] = f.get(event, ctx) if f.get else getattr(event, f.attr)
    return out


class WriteRefused(Exception):
    """Raised when a write is not allowed, or a value does not clean.

    Carries the field name so a caller can build the 400 body the frontend already expects, rather
    than a generic message nobody can act on.
    """

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def apply_event_writes(event, data, *, actor=None, role=None, table=None):
    """Apply the writable subset of `data` to `event`, returning the names that actually changed.

    Each rule mirrors behaviour that already exists somewhere in views.py:

      - a key ABSENT from data is left alone, so a partial edit stays partial (this is exactly what
        edit_event's 45 `if "x" in request.data` guards do)
      - a key the actor may not write is REFUSED loudly rather than ignored quietly, so a
        permission mistake surfaces in testing instead of in production
      - `clean` runs before the assignment, and a ValueError from it becomes a WriteRefused naming
        the field
      - an unchanged value is not reported as changed, which is what the admin edit page's confirm
        dialog wants

    NOTHING is applied if any key is refused: the whole payload is validated first, so a 400 never
    leaves the event half-written.

    Does NOT save. The caller decides, because create and edit differ on when the row exists.
    """
    if role is None:
        role = role_of(actor, event) if actor is not None else PUBLIC
    rows = EVENT_FIELDS if table is None else table

    # Pass 1: permission-check and clean EVERYTHING before touching the event.
    pending = []
    for f in rows:
        if f.name not in data:
            continue
        if not satisfies(role, f.write):
            raise WriteRefused(f.name, f"You may not set {f.name} on this event.")
        raw = data[f.name]
        if f.clean:
            try:
                value = f.clean(raw)
            except ValueError as exc:
                raise WriteRefused(f.name, str(exc))
        else:
            value = raw
        pending.append((f, value))

    # Pass 2: assign.
    changed = []
    for f, value in pending:
        if getattr(event, f.attr) != value:
            setattr(event, f.attr, value)
            changed.append(f.name)
    return changed


def with_defaults(data):
    """Fill in the declared default for any field the request omitted.

    Only used on CREATE. Edit deliberately does NOT do this: an omitted key there means "leave it
    alone", and applying a default would silently reset fields the editor never mentioned.
    """
    filled = dict(data)
    for f in EVENT_FIELDS:
        if f.name not in filled and f.default is not None:
            filled[f.name] = f.default()
    return filled
