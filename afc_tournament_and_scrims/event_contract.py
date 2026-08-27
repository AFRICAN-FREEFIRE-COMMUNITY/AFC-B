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
    registration_open_date COLUMN under the key "registration_start_date", which a
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


# ── cleaners ──────────────────────────────────────────────────────────────────────────────────
# Each of these reproduces exactly what edit_event did for that field before the conversion, and
# the behaviour is pinned by test_event_write_behaviour.py, which was written and run GREEN against
# the unconverted endpoints first.
#
# They raise ValueError to reject. apply_event_writes turns that into a WriteRefused carrying the
# field name, and the endpoint turns THAT into the 400 the frontend already expects.
#
# THE RULE THEY ALL FOLLOW: ask "did this parse as the right type", never "does it contain
# anything". An empty list, a zero and an empty string are all real answers. Gating on truthiness
# is what took event creation down on 2026-08-26.


def _clean_bool(raw):
    """A checkbox from a multipart form arrives as the STRING "true" or "false", and "false" is
    truthy, so a plain bool() would read every unchecked box as checked."""
    from .views import _as_bool
    return _as_bool(raw)


def _clean_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError("must be a whole number.")


def _clean_waitlist_capacity(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError("waitlist_capacity must be an integer.")


def _clean_cash_value(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError("prizepool_cash_value must be a number.")


def _clean_date(raw):
    from django.utils.dateparse import parse_date
    return parse_date(raw) if isinstance(raw, str) else raw


def _clean_optional_str(raw):
    """A cleared time or timezone stores NULL, not the empty string, so the column stays nullable
    in the way every reader already assumes."""
    return raw or None


def _clean_stripped_or_none(raw):
    return (raw or "").strip() or None


def _clean_prizepool(raw):
    return str(raw)


def _clean_currency_code(raw):
    return (raw or "USD").upper()[:3]


def _clean_prize_distribution(raw):
    import json as _json
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            raise ValueError("prize_distribution must be a JSON object.")
    if not isinstance(raw, dict):
        raise ValueError("prize_distribution must be a JSON object.")
    return raw


def _clean_registration_type(raw):
    if raw not in ("free", "paid"):
        raise ValueError("registration_type must be 'free' or 'paid'.")
    return raw


def _clean_registration_fee(raw):
    from decimal import Decimal, InvalidOperation
    if raw in (None, "", "null"):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise ValueError("registration_fee must be a number.")


def _clean_seed_trigger(raw):
    from .views import _clean_auto_seed_trigger
    return _clean_auto_seed_trigger(raw)


def _clean_min_letter_avatars(raw):
    from .views import _parse_min_letter_avatars
    return _parse_min_letter_avatars(raw)


def _clean_connections(raw):
    from .views import _clean_required_connections
    return _clean_required_connections(raw)


def _clean_waitlist_mode(raw):
    """An unknown mode is IGNORED rather than refused, which is what edit_event did: a bad payload
    must not be able to corrupt how slots are assigned, but it also never 400ed for this."""
    from .models import Event
    if raw in dict(Event.WAITLIST_MODE_CHOICES):
        return raw
    raise _KeepExisting


class _KeepExisting(Exception):
    """Sentinel: this value is not acceptable, but the field keeps what it already had.

    Only waitlist_mode uses it, and only because edit_event silently ignored unknown modes rather
    than rejecting them. Preserved rather than tidied into a 400, because changing a silent ignore
    into an error is a behaviour change the frontend was never written for.
    """


# ── the field table ────────────────────────────────────────────────────────────────
# Order here is the order the readers used, kept so a reviewer can diff this against the old
# literal line by line. Every VALUE is verified against a golden captured from the old code, so a
# transcription slip fails the suite rather than reaching the frontend.
#
# `get` receives (event, ctx). ctx["request"] is the DRF request (needed for absolute URIs) and
# ctx["extra"] carries values the calling endpoint already queried for, so the contract never
# repeats a query the reader has already run.
EVENT_FIELDS = [
    # ── identity and shape ──
    Field("event_id", read=PUBLIC),
    Field("competition_type", read=PUBLIC, write=ORGANIZER),
    Field("participant_type", read=PUBLIC, write=ORGANIZER),
    Field("event_type", read=PUBLIC),
    Field("max_teams_or_players", read=PUBLIC, write=ORGANIZER, clean=_clean_int),
    Field("event_name", read=PUBLIC, write=ORGANIZER),
    Field("event_mode", read=PUBLIC, write=ORGANIZER),

    # ── dates ──
    Field("start_date", read=PUBLIC, write=ORGANIZER, clean=_clean_date),
    Field("end_date", read=PUBLIC, write=ORGANIZER, clean=_clean_date),
    Field("registration_open_date", read=PUBLIC, write=ORGANIZER, clean=_clean_date),
    Field("registration_end_date", read=PUBLIC, write=ORGANIZER, clean=_clean_date),
    # Roster-edit window (owner 2026-06-15): the team-facing UI uses these to show whether captains
    # may currently edit their roster, and until when. roster_edit_open auto-derives from
    # roster_edit_until versus now (see Event.roster_edit_open / set_roster_edit_window).
    Field("roster_edit_until", read=PUBLIC),
    Field("roster_edit_open", read=PUBLIC),

    # ── money ──
    Field("prizepool", read=PUBLIC, write=ORGANIZER, clean=_clean_prizepool),
    # Echo the cash value AND its currency (owner bug 2026-07-02): the edit form seeds from this
    # payload, and without both keys a saved value came back undefined and looked like it vanished.
    Field("prizepool_cash_value", read=PUBLIC, write=ORGANIZER, clean=_clean_cash_value),
    Field("prize_currency", read=PUBLIC, get=lambda e, ctx: getattr(e, "prize_currency", None)),
    Field("prize_distribution", read=PUBLIC, write=ORGANIZER, clean=_clean_prize_distribution),
    # Paid registration (feature "paid-events"): the event page decides free versus paid, and the fee.
    Field("registration_type", read=PUBLIC, write=ORGANIZER, clean=_clean_registration_type),
    Field("registration_fee", read=PUBLIC, write=ORGANIZER, clean=_clean_registration_fee),
    Field("registration_fee_currency", read=PUBLIC, write=ORGANIZER, clean=_clean_currency_code),
    # Per-country payment (owner 2026-06-24): an anonymous viewer has no country to price against,
    # so your_registration_fee is null for them and the page shows the base fee plus "varies by
    # country" when rules exist. A signed-in reader passes the real number through extra.
    Field("country_payment_rules", read=PUBLIC),
    Field("your_registration_fee", read=PUBLIC,
          get=lambda e, ctx: ctx["extra"].get("your_registration_fee")),

    # ── content ──
    # Per-event results visibility (owner 2026-06-29): false withholds the standings and the public
    # Results view shows "Results not published yet". See set_results_visibility.
    Field("results_published", read=PUBLIC),
    Field("event_rules", read=PUBLIC, write=ORGANIZER),
    # What the tournament IS, in the organizer's words (owner 2026-08-05, item 26). Blank on most
    # events, and the public About block simply does not render until somebody writes one.
    Field("event_description", read=PUBLIC, write=ORGANIZER),
    Field("public_sponsors", read=PUBLIC, get=lambda e, ctx: ctx["extra"]["public_sponsors"]),
    # Read-time display status: a started event reads as "ongoing" without waiting on the sweep.
    Field("event_status", read=PUBLIC, write=ORGANIZER, get=lambda e, ctx: ctx["extra"]["event_status"]),

    # ── PROVENANCE (owner 2026-08-20, external results import) ──
    # NULL for everything AFC ran. A timestamp means the results came from an external organizer's
    # published standings rather than being played on AFC, and the event page shows a marker saying
    # so. Driven off the stored timestamp rather than its own switch, so it cannot drift out of
    # step with whether an import actually happened.
    Field("results_imported", read=PUBLIC, get=lambda e, ctx: e.results_imported_at is not None),
    Field("results_imported_at", read=PUBLIC),

    Field("registration_link", read=PUBLIC, write=ORGANIZER),
    # Tournament tier (tier_1/2/3) so the event CARD can show a tier badge (owner 2026-06-29).
    Field("tournament_tier", read=PUBLIC),

    # ── media and organization, all request-dependent (absolute URIs) ──
    Field("event_banner_url", read=PUBLIC, get=lambda e, ctx: (
        ctx["request"].build_absolute_uri(e.event_banner.url) if e.event_banner else None
    )),
    # Organizing org, null for AFC-native events. Exposed so the public tournament page can build
    # the link-embed fallback chain (banner, then ORG LOGO, then the AFC default, owner 2026-06-14)
    # and fill the SportsEvent JSON-LD organizer. Read by app/(user)/tournaments/[slug]/page.tsx.
    Field("organization_name", read=PUBLIC,
          get=lambda e, ctx: e.organization.name if e.organization else None),
    Field("organization_slug", read=PUBLIC,
          get=lambda e, ctx: e.organization.slug if e.organization else None),
    Field("organization_logo", read=PUBLIC, get=lambda e, ctx: (
        ctx["request"].build_absolute_uri(e.organization.logo.url)
        if (e.organization and e.organization.logo)
        else None
    )),
    Field("uploaded_rules_url", read=PUBLIC, get=lambda e, ctx: (
        ctx["request"].build_absolute_uri(e.uploaded_rules.url) if e.uploaded_rules else None
    )),

    Field("number_of_stages", read=PUBLIC, write=ORGANIZER, clean=_clean_int),
    Field("created_at", read=PUBLIC),
    Field("stream_channels", read=PUBLIC,
          get=lambda e, ctx: list(e.stream_channels.values_list("channel_url", flat=True))),
    Field("is_public", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),

    # ── Discord registration gate (per-event) ──
    # require_discord is its own switch and means MORE than required_connections does: connected
    # AND a member of the event's server, with a paired invite link. A blank discord_server_id
    # means the main AFC guild.
    Field("require_discord", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("discord_server_id", read=PUBLIC, write=ORGANIZER, clean=_clean_stripped_or_none),
    Field("discord_invite_link", read=PUBLIC, write=ORGANIZER, clean=_clean_stripped_or_none),

    # ── sponsorship ──
    Field("is_sponsored", read=PUBLIC),
    Field("sponsor_name", read=PUBLIC, write=ORGANIZER),
    Field("sponsor_field_label", read=PUBLIC, write=ORGANIZER),
    Field("sponsor_requirement_description", read=PUBLIC, write=ORGANIZER),
    Field("sponsors", read=PUBLIC, get=lambda e, ctx: [
        {
            "sponsor_id": se.sponsor.user_id,
            "sponsor_name": se.sponsor.full_name,
            "sponsor_username": se.sponsor.username,
        }
        for se in ctx["extra"]["sponsors"]
    ]),

    # ── times, and the derived registration window ──
    Field("registration_start_time", read=PUBLIC, write=ORGANIZER, clean=_clean_optional_str),
    Field("registration_end_time", read=PUBLIC, write=ORGANIZER, clean=_clean_optional_str),
    Field("event_start_time", read=PUBLIC, write=ORGANIZER, clean=_clean_optional_str),
    Field("event_end_time", read=PUBLIC, write=ORGANIZER, clean=_clean_optional_str),
    Field("registration_opens_at", read=PUBLIC,
          get=lambda e, ctx: _registration_window(e)[0].isoformat()),
    Field("registration_closes_at", read=PUBLIC,
          get=lambda e, ctx: _registration_window(e)[1].isoformat()),
    Field("registration_is_open", read=PUBLIC, get=lambda e, ctx: _registration_is_open(e)),

    # ── waitlist ──
    Field("is_waitlist_enabled", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("waitlist_mode", read=PUBLIC, write=ORGANIZER, clean=_clean_waitlist_mode),

    # ── registration requirements ──
    Field("require_team_logo", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("require_esport_images", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("require_player_uid", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("require_player_profile_image", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    Field("require_whatsapp", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),
    # A list, normalised on the way out so a NULL column reads as [] rather than None. NEVER gate
    # this on truthiness: empty is a real answer and means "no requirement" (outage, 2026-08-26).
    Field("required_connections", read=PUBLIC, write=ORGANIZER, clean=_clean_connections,
          get=lambda e, ctx: list(e.required_connections or [])),
    Field("allow_team_result_submissions", read=PUBLIC, write=ORGANIZER, clean=_clean_bool),

    # ── capacity snapshot, counted by the endpoint with the same rule register_for_event uses ──
    Field("waitlist_capacity", read=PUBLIC, write=ORGANIZER, clean=_clean_waitlist_capacity),
    Field("registered_count", read=PUBLIC, get=lambda e, ctx: ctx["extra"]["active_registered"]),
    Field("is_full", read=PUBLIC,
          get=lambda e, ctx: ctx["extra"]["active_registered"] >= e.max_teams_or_players),
    Field("co_organizers", read=PUBLIC, get=lambda e, ctx: [
        {
            "name": c.organization.name,
            "slug": c.organization.slug,
            "logo": (ctx["request"].build_absolute_uri(c.organization.logo.url)
                     if c.organization.logo else None),
        }
        for c in e.co_organizers.filter(status="accepted").select_related("organization")
    ]),
    # ── SIGNED-IN ONLY ────────────────────────────────────────────────────────────────────────
    # Everything below appeared in get_event_details and NOT in the logged-out reader. Verified
    # against both captured goldens on 2026-08-26: the public payload is an EXACT subset of the
    # signed-in one, so PLAYER only ever ADDS. Nothing is withheld from a signed-in viewer that a
    # logged-out visitor can see.
    #
    # NOT here, deliberately: is_registered, my_waiver, my_invitation, your_team_roster_edit_open,
    # your_team_roster_edit_until, your_team_stage_over and waitlist_competitors. Those describe
    # the VIEWER's relationship to the event rather than the event itself, so they are computed by
    # the endpoint and merged in there. A field is only in this table if it belongs to the event.
    Field("slug", read=PLAYER),
    Field("timezone", read=PLAYER, write=ORGANIZER, clean=_clean_optional_str),
    Field("organization_id", read=PLAYER),
    # Geo restriction: WHO may register. Withheld from an anonymous visitor because it describes
    # the event's gating rather than the event, and the public page has nobody to apply it to.
    #
    # READ-ONLY here on purpose. edit_event validates these three TOGETHER: the mode and the country
    # list are required only when the restriction is enabled, and both are cleared when it is set to
    # "none". That is a cross-field rule, so making them independently writable would let a caller
    # set a mode with no countries, or leave a stale country list behind a disabled restriction.
    Field("registration_restriction", read=PLAYER),
    Field("restriction_mode", read=PLAYER),
    Field("restricted_countries", read=PLAYER),
    # Auto-seeding: whether the entry stage seeds itself when the event starts, and on what.
    Field("auto_seed_on_start", read=PLAYER, write=ORGANIZER, clean=_clean_bool),
    Field("auto_seed_trigger", read=PLAYER, write=ORGANIZER, clean=_clean_seed_trigger),
    # Whether an admin overrode the computed tournament tier (see the tier rules engine).
    Field("tier_overridden", read=PLAYER),
    # Letter-avatars registration gate (feature #7, owner 2026-06-29): 0 means off.
    Field("min_letter_avatars", read=PLAYER, write=ORGANIZER, clean=_clean_min_letter_avatars),
    Field("waitlist_discord_role_id", read=PLAYER, write=ORGANIZER),
    # WRITE-ONLY. No reader exposes is_draft, so read=NOBODY, but edit_event lets an organizer
    # flip it, so it still needs a row: a field the contract cannot write is a field that has to be
    # hand-assigned somewhere else, which is the thing this module exists to stop.
    Field("is_draft", read=NOBODY, write=ORGANIZER, clean=_clean_bool),
    # ── ADMIN METRICS ENDPOINT ONLY ───────────────────────────────────────────────────────────
    # get_event_details_for_admin emits the registration_open_date COLUMN under a DIFFERENT KEY.
    # Verified in the code on 2026-08-26 and pinned by its own golden test. Whatever admin surface
    # reads this expects "registration_start_date", so the name is preserved DELIBERATELY rather
    # than tidied up: renaming it back would look like a cleanup and would break that surface.
    #
    # This one row is also the reason a DRF ModelSerializer was rejected for the whole job. A
    # serializer derived from the model cannot produce a key its column is not called.
    Field("registration_start_date", read=ADMIN, source="registration_open_date"),
]


# ── the subsets get_event_details_for_admin asks for ──────────────────────────────────────────
# That endpoint is a registration METRICS view that re-lists some of the event's fields on its way
# past, in two sub-blocks. Naming the members explicitly is what keeps that payload stable when a
# new field is added to the contract for the player page: a new field does NOT silently appear in
# the admin payload.
#
# NOT in this list, on purpose: "prizepool". The metrics block emits it as a NUMBER
# (float(event.prizepool), falling back to the raw value), while the readers emit the raw string.
# Two endpoints, two types, same key. Letting the contract supply it here would change the type
# the admin page receives, so the metrics block keeps its own.
ADMIN_OVERVIEW_FIELDS = [
    "event_id", "event_name", "roster_edit_until", "roster_edit_open", "prize_distribution",
    "is_public", "is_sponsored", "sponsor_name", "sponsor_field_label",
    "sponsor_requirement_description", "sponsors", "is_waitlist_enabled", "require_team_logo",
    "require_esport_images", "require_player_uid", "require_player_profile_image",
    "require_whatsapp", "required_connections", "allow_team_result_submissions",
    "waitlist_capacity", "waitlist_discord_role_id", "waitlist_mode", "event_start_time",
    "event_end_time", "timezone",
]

ADMIN_TIMELINE_FIELDS = [
    "registration_start_date",   # the renamed key, see the Field above
    "registration_end_date",
    "registration_start_time",
    "registration_end_time",
]


def _registration_window(event):
    """views.registration_window_instants, imported lazily to keep this module import-cycle free."""
    from .views import registration_window_instants
    return registration_window_instants(event)


def _registration_is_open(event):
    """views.registration_is_open, imported lazily for the same reason."""
    from .views import registration_is_open
    return registration_is_open(event)


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
        if f.write == NOBODY:
            # NEVER writable through the API by anyone, so its presence in the payload is not a
            # permission problem and must not 400. Ordinary traffic carries these: edit_event is
            # looked up BY event_id, so every single edit request contains it, and refusing it
            # would reject every edit. The distinction that matters:
            #   write=NOBODY        structural, nobody may ever set it   -> ignore it
            #   write above my rung a real permission boundary           -> refuse loudly
            # Refusing the second is what surfaces a permission mistake in testing rather than in
            # production. Refusing the first would only reject normal requests.
            continue
        if not satisfies(role, f.write):
            raise WriteRefused(f.name, f"You may not set {f.name} on this event.")
        raw = data[f.name]
        if f.clean:
            try:
                value = f.clean(raw)
            except _KeepExisting:
                # The cleaner judged the value unusable but the field keeps what it had, with no
                # error. Only waitlist_mode does this, preserving edit_event's silent ignore of an
                # unknown mode. See _clean_waitlist_mode.
                continue
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


# ── DUPLICATION ───────────────────────────────────────────────────────────────────────────────
# A duplicate inherits the event's SHAPE. It never inherits identity, results, or history.
#
# WHY THIS IS DRIVEN OFF THE MODEL AND NOT OFF EVENT_FIELDS: a copy has to carry INTERNAL config
# too (scoring switches, check-in settings), and those are deliberately absent from the contract
# because no reader exposes them. Reading the model means the default is INHERIT, and dropping a
# field has to be a deliberate line in the list below.
#
# That direction matters. duplicate_event used to build the copy from a hand-typed list of 51
# keyword arguments whose own comment claimed it "mirrors create_event ... so the two stay in
# lockstep". It had drifted TWICE: once on the require_* gates (patched by hand, comment "these
# were previously dropped, so a duplicated event silently lost its require_* toggles"), and again
# by 2026-08-26 on require_discord, discord_server_id, discord_invite_link, timezone,
# waitlist_mode, auto_seed_on_start and auto_seed_trigger. Nothing failed either time, because a
# missing keyword argument just takes the column default.
DUPLICATE_EXCLUDED = {
    # Identity. The copy is a different row with a different name and its own slug.
    "event_id",
    "slug",
    "event_name",
    "creator",
    "created_at",
    "updated_at",
    # Lifecycle. A clone is always a fresh unpublished draft, never a finished event. All three
    # are passed explicitly by duplicate_event, so they MUST stay excluded here or
    # Event.objects.create() receives the same keyword twice and raises TypeError.
    "event_status",
    "is_draft",
    "is_public",
    # The four date fields are SHIFTED rather than copied, by _cloned_dates, so that a clone of a
    # finished event does not sit in the past and get re-stamped "completed" by the status sweep
    # (owner backlog item 27). They are supplied by the endpoint, not from here.
    "start_date",
    "end_date",
    "registration_open_date",
    "registration_end_date",
    # Results, and the gates that publish them. A copy has played nothing.
    "results_published",
    "rankings_verified",
    "partner_published",
    "results_imported_at",
    "results_imported_by",
    "imported_results_visible_on_profiles",
    "imported_results_count_in_profile_stats",
    "auto_complete_suppressed",
    "auto_seeded_at",
    # Per-event secrets and live-broadcast targeting, which point at the SOURCE event's stages and
    # groups. Copying them would aim the clone's overlays at another event's rows.
    "overlay_token",
    "broadcast_scope",
    "broadcast_stage_id",
    "broadcast_group_id",
    "broadcast_group_ids",
    # Windows and overrides that are meaningful only for the RUN that is happening, not for the
    # shape of the event. The check-in WINDOW is a pair of datetimes tied to the source event's
    # schedule, so it is excluded while the check-in SWITCH is carried, the same way the dates are
    # shifted while the times are copied.
    "roster_edit_until",
    "tier_overridden",
    "checkin_start",
    "checkin_end",
}


def duplicate_field_values(source):
    """Every Event column a copy inherits, read straight off the model.

    Returns a dict keyed by ATTNAME, so a foreign key comes back as `organization_id` and is
    assigned without fetching the related row.

    Lists and dicts are COPIED, never aliased: sharing the same list object between the source and
    the clone would mean editing one silently edited the other.
    """
    from .models import Event

    values = {}
    for field in Event._meta.concrete_fields:
        if field.name in DUPLICATE_EXCLUDED:
            continue
        value = getattr(source, field.attname)
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        values[field.attname] = value
    return values


# ── ACCOUNTING FOR EVERY COLUMN ───────────────────────────────────────────────────────────────
# The rule the checker enforces is NOT "every field must be exposed". Plenty of Event's columns are
# internal machinery and are correctly invisible. The rule is that every column is accounted for
# SOMEWHERE: declared in EVENT_FIELDS, exposed through a derived key, or named below as internal.
# A new column that is in none of those fails the suite, which is what stops the next field being
# half-added.

# Columns no reader exposes directly, but which reach the wire through a DERIVED key. Listed with
# the keys that expose them so a reader can follow it without grepping.
DERIVED_FROM = {
    "event_banner": ("event_banner_url",),
    "uploaded_rules": ("uploaded_rules_url",),
    "organization": ("organization_id", "organization_name", "organization_slug",
                     "organization_logo"),
}

# Columns no reader exposes at all, deliberately. Broadcast targeting and the overlay token are
# operational; check-in, scoring config and draft state are internal; the currency snapshot fields
# are working values behind prizepool_cash_value.
INTERNAL_FIELDS = {
    "auto_complete_suppressed",
    "auto_seeded_at",
    "broadcast_group_id",
    "broadcast_group_ids",
    "broadcast_scope",
    "broadcast_stage_id",
    "checkin_enabled",
    "checkin_end",
    "checkin_start",
    "count_flagged_kills",
    "creator",
    "imported_results_count_in_profile_stats",
    "imported_results_visible_on_profiles",
    "mvp_config",
    "overlay_token",
    "partner_published",
    "prizepool_ngn_value",
    "rankings_verified",
    "results_imported_by",
    "tie_breakers",
    "updated_at",
    "usd_to_ngn_rate",
}


def unaccounted_fields():
    """Event columns that are in neither EVENT_FIELDS, DERIVED_FROM, nor INTERNAL_FIELDS.

    Used by both the completeness test and tools/check_event_contract.py, so the two cannot
    disagree about what "accounted for" means.
    """
    from .models import Event

    model_fields = {f.name for f in Event._meta.concrete_fields}
    declared = {f.attr for f in EVENT_FIELDS}
    return sorted(model_fields - declared - set(DERIVED_FROM) - INTERNAL_FIELDS)


def duplicate_field_names():
    """The Event columns a duplicate inherits, by field name. For the checker's second question."""
    from .models import Event

    return sorted(f.name for f in Event._meta.concrete_fields
                  if f.name not in DUPLICATE_EXCLUDED)
