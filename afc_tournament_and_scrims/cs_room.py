"""
Clash-Squad ROOM SETTINGS logic (owner 2026-08-12, spec: WEBSITE/tasks/cs-room-settings-spec.md).

WHAT LIVES HERE: everything that reasons about a room configuration - building a blank one from
the catalogue, validating what an organizer submitted, saving it against a scope, RESOLVING which
configuration applies to a given match, turning one into the short summary a player reads, and
applying a preset. The endpoints (cs_room_views.py) stay thin and only do auth + shape.

THE ONE IDEA WORTH REMEMBERING - inheritance, not duplication:
a configuration is attached to a SCOPE (event / stage / group / match) and a match resolves the
narrowest one that exists: match -> group -> stage -> event. So "apply these settings to every
match in this stage" is ONE stage-scoped row, and "except the grand final, which is best-of-13"
is one extra match-scoped row. Nothing is copied per match, and the resolved answer always says
where it came from so the UI can print "inherited from Stage 1".

HOW IT CONNECTS
  - option lists + Free Fire defaults: cs_room_catalogue.py (the only place that knows the game)
  - models: CSRoomConfig / CSRoomPreset (models.py)
  - endpoints: cs_room_views.py, mounted under events/ in urls.py
  - head_to_head.report_result calls max_wins_for_match() so a set score cannot exceed the
    best-of the room is actually configured for (previously the only guard was a flat cap of 99)
  - FE: lib/csRoom.ts -> components/cs-room-settings.tsx (editor) and the player-facing card
"""
from django.db import transaction

from . import cs_room_catalogue as cat
from .models import CSRoomConfig, CSRoomPreset


class RoomConfigError(Exception):
    """Caller-facing validation failure. cs_room_views turns the message into a 400, so it must
    stay readable by a non-technical organizer."""


# The four scopes, NARROWEST FIRST. This order is the resolution order and the display order, so
# it is defined once here rather than being re-spelled in each function.
SCOPES = ("match", "group", "stage", "event")

# Which model field on CSRoomConfig holds each scope's foreign key.
SCOPE_FIELD = {
    "match": "h2h_match",
    "group": "group",
    "stage": "stage",
    "event": "event",
}

# Every settings column shared by a config and a preset (CSRoomSettingsBase). Listed once so the
# serializer, the validator and apply_preset cannot drift apart.
CORE_FIELDS = (
    "rounds", "economy", "special_mode", "special_airdrop", "hp", "ep",
    "movement_speed", "jump_height", "environment", "map_name", "preset_key",
)
JSON_FIELDS = ("toggles", "store", "round_economy", "economy_events", "areas")
SETTINGS_FIELDS = CORE_FIELDS + JSON_FIELDS

# Fields that only a scoped config has (a preset is not attached to anything, so it has no room
# ID to hand out and nothing to publish).
CONFIG_ONLY_FIELDS = ("room_id", "room_password", "notes", "is_published")


# ── defaults ─────────────────────────────────────────────────────────────────────────────────
def blank_settings():
    """A fresh Free Fire room as a plain dict: the catalogue's defaults for everything.

    Used when an organizer opens the editor on a scope that has no configuration yet, and as the
    base a preset is applied on top of.
    """
    rounds = 7
    map_name = "nexterra"
    return {
        "rounds": rounds,
        "economy": "500",
        "special_mode": "no",
        "special_airdrop": "no",
        "hp": 200,
        "ep": 0,
        "movement_speed": 100,
        "jump_height": 100,
        "environment": "day",
        "map_name": map_name,
        "preset_key": "",
        "toggles": cat.default_toggles(),
        "store": cat.default_store(),
        "round_economy": cat.default_round_economy(rounds),
        "economy_events": cat.default_economy_events(),
        "areas": cat.default_areas(rounds, map_name),
    }


# ── validation ───────────────────────────────────────────────────────────────────────────────
def _choice_values(choices):
    """The value half of a catalogue (value, label) list."""
    return {value for value, _label in choices}


def _as_int(raw, field, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RoomConfigError(f"{field} must be a whole number.")
    if value < low or value > high:
        raise RoomConfigError(f"{field} must be between {low} and {high}.")
    return value


def validate_settings(data):
    """Check and coerce a submitted settings dict, returning ONLY the recognised keys.

    Every value is checked against the catalogue rather than trusted, because this payload comes
    straight off a form: an unknown map would silently produce a room nobody can join, and a
    negative price would print as a bargain. Anything the caller omits keeps whatever the row
    already had (the caller merges), so this validates a PARTIAL payload.

    Raises RoomConfigError with a message an organizer can act on.
    """
    if not isinstance(data, dict):
        raise RoomConfigError("Room settings must be an object.")

    clean = {}

    if "rounds" in data:
        rounds = _as_int(data["rounds"], "Rounds", 1, 99)
        if rounds not in cat.ROUND_CHOICES:
            raise RoomConfigError(
                f"Rounds must be one of {', '.join(str(r) for r in cat.ROUND_CHOICES)}.")
        clean["rounds"] = rounds

    for field, choices in (
        ("economy", cat.ECONOMY_CHOICES),
        ("special_mode", cat.SPECIAL_MODE_CHOICES),
        ("special_airdrop", cat.SPECIAL_AIRDROP_CHOICES),
        ("environment", cat.ENVIRONMENT_CHOICES),
        ("map_name", cat.MAP_CHOICES),
    ):
        if field in data:
            value = str(data[field] or "")
            if value not in _choice_values(choices):
                raise RoomConfigError(f"'{value}' is not a valid {field.replace('_', ' ')}.")
            clean[field] = value

    for field, allowed in (
        ("hp", cat.HP_CHOICES),
        ("ep", cat.EP_CHOICES),
        ("movement_speed", cat.MOVEMENT_SPEED_CHOICES),
        ("jump_height", cat.JUMP_HEIGHT_CHOICES),
    ):
        if field in data:
            value = _as_int(data[field], field.replace("_", " ").title(), 0, 1000)
            if value not in allowed:
                raise RoomConfigError(
                    f"{field.replace('_', ' ').title()} must be one of "
                    f"{', '.join(str(a) for a in allowed)}.")
            clean[field] = value

    if "preset_key" in data:
        # Free text on purpose: it is a LABEL saying what this was built from, and an
        # organization preset's name is not in the catalogue. Length-capped to the column.
        clean["preset_key"] = str(data["preset_key"] or "")[:40]

    if "toggles" in data:
        raw = data["toggles"]
        if not isinstance(raw, dict):
            raise RoomConfigError("Toggles must be an object of on/off values.")
        # Unknown keys are DROPPED rather than rejected: a client running an older catalogue
        # should not be blocked from saving, it should just not invent settings.
        clean["toggles"] = {k: bool(raw[k]) for k in cat.TOGGLES if k in raw}

    if "store" in data:
        raw = data["store"]
        if not isinstance(raw, dict):
            raise RoomConfigError("The store must be an object keyed by item.")
        known = {code for code, _l, _p in (cat.STORE_WEAPONS + cat.STORE_ITEMS)}
        store = {}
        for code, entry in raw.items():
            if code not in known or not isinstance(entry, dict):
                continue
            price = _as_int(entry.get("price", 0), f"Price for {code}", 0, 999_999)
            store[code] = {"enabled": bool(entry.get("enabled", True)), "price": price}
        clean["store"] = store

    if "round_economy" in data:
        raw = data["round_economy"]
        if not isinstance(raw, dict):
            raise RoomConfigError("Round economy must be an object keyed by round number.")
        clean["round_economy"] = {
            str(int(k)): _as_int(v, f"Round {k} starting cash", 0, 999_999)
            for k, v in raw.items() if str(k).isdigit()
        }

    if "economy_events" in data:
        raw = data["economy_events"]
        if not isinstance(raw, dict):
            raise RoomConfigError("Economy events must be an object.")
        clean["economy_events"] = {
            k: _as_int(raw[k], cat.ECONOMY_EVENTS[k][0], 0, 999_999)
            for k in cat.ECONOMY_EVENTS if k in raw
        }

    if "areas" in data:
        raw = data["areas"]
        if not isinstance(raw, dict):
            raise RoomConfigError("Areas must be an object keyed by round number.")
        # Areas are validated against the map being saved, or - when the map is unchanged in this
        # payload - against every map, since we cannot see the stored row from here. The endpoint
        # re-validates after merging, which is where a map/area mismatch is actually caught.
        target_map = clean.get("map_name") or data.get("map_name")
        if target_map:
            allowed = {code for code, _l in cat.MAP_AREAS.get(target_map, [])}
        else:
            allowed = {code for areas in cat.MAP_AREAS.values() for code, _l in areas}
        areas = {}
        for k, v in raw.items():
            if not str(k).isdigit():
                continue
            value = str(v or "")
            if allowed and value and value not in allowed:
                raise RoomConfigError(f"'{value}' is not an area on that map.")
            areas[str(int(k))] = value
        clean["areas"] = areas

    return clean


def _validate_merged(values):
    """Cross-field repairs that only make sense once the payload is merged onto the stored row.

    Two rules, both about the per-ROUND documents following the fields they depend on:

    1. ROUNDS drives how many entries round_economy and areas need. A caller that sends only
       {"rounds": 13} - the create-event wizard, or any API client - would otherwise end up with a
       13-round room carrying 7 rounds of starting cash and 7 areas, so rounds 8-13 would have no
       economy and no area at all. Grow (repeating the last value, as Free Fire does) or trim to
       match. Caught by a test rather than by anyone playing a broken room, because the editor UI
       happens to regrow them client-side and hid this.
    2. Every per-round AREA must belong to the map that ends up saved. A payload that changes the
       map without resending the areas would leave a room set to play Solara's Windmill on
       Kalahari; refill from the new map's own list rather than refusing the save, because an
       organizer changing the map wants the map changed, not an error.
    """
    rounds = int(values.get("rounds") or 7)
    map_name = values.get("map_name", "")

    # ── 1. per-round documents follow the round count ──
    economy = dict(values.get("round_economy") or {})
    if len(economy) != rounds:
        defaults = cat.default_round_economy(rounds)
        values["round_economy"] = {
            str(n): economy.get(str(n), defaults[str(n)]) for n in range(1, rounds + 1)
        }

    areas = dict(values.get("areas") or {})
    allowed = {code for code, _l in cat.MAP_AREAS.get(map_name, [])}
    if not allowed:
        # A map with no area list (Bermuda) constrains nothing and has no areas to fill.
        values["areas"] = {}
        return values

    # ── 2. areas belong to the saved map, and there is one per round ──
    if any(v and v not in allowed for v in areas.values()) or len(areas) != rounds:
        defaults = cat.default_areas(rounds, map_name)
        values["areas"] = {
            str(n): (areas.get(str(n)) if areas.get(str(n)) in allowed else defaults[str(n)])
            for n in range(1, rounds + 1)
        }
    return values


# ── serialization ────────────────────────────────────────────────────────────────────────────
def settings_payload(obj):
    """The settings half of a config or preset, as the FE consumes it."""
    return {field: getattr(obj, field) for field in SETTINGS_FIELDS}


def config_payload(config, *, include_room_credentials=True):
    """One CSRoomConfig for the API.

    include_room_credentials=False blanks the room ID and password. Used on the PUBLIC read for a
    configuration that is not published yet, so a spectator page can show the ruleset (which is
    useful to everyone) without handing out the door code before the organizer opens it.
    """
    if config is None:
        return None
    data = settings_payload(config)
    data.update({
        "cs_room_config_id": config.cs_room_config_id,
        "scope": config.scope,
        "scope_object_id": config.scope_object_id,
        "notes": config.notes,
        "is_published": config.is_published,
        "room_id": config.room_id if include_room_credentials else "",
        "room_password": config.room_password if include_room_credentials else "",
        "has_room_credentials": bool(config.room_id or config.room_password),
        "updated_at": config.updated_at,
    })
    return data


def preset_payload(preset):
    data = settings_payload(preset)
    data.update({
        "cs_room_preset_id": preset.cs_room_preset_id,
        "name": preset.name,
        "description": preset.description,
        "organization_id": preset.organization_id,
        "is_builtin": preset.is_builtin,
    })
    return data


def summary(config):
    """The short human line a player reads before opening the full settings.

    Deliberately tiny: the things that change how you actually play. Values are CODES, not
    labels - the frontend already has the catalogue and does the translating, so no English
    leaks out of the backend into a French player's screen.
    """
    if config is None:
        return None
    toggles = config.toggles or {}
    return {
        "rounds": config.rounds,
        "map_name": config.map_name,
        "economy": config.economy,
        "hp": config.hp,
        "special_mode": config.special_mode,
        "environment": config.environment,
        # The four organizers argue about most often, so they sit in the summary rather than
        # three taps deep in the full list.
        "headshot": bool(toggles.get("headshot", False)),
        "character_skill": bool(toggles.get("character_skill", True)),
        "loadout": bool(toggles.get("loadout", True)),
        "gun_attributes": bool(toggles.get("gun_attributes", True)),
        "wins_needed": wins_needed(config.rounds),
    }


# ── best-of ──────────────────────────────────────────────────────────────────────────────────
def wins_needed(rounds):
    """Round wins needed to take a set of `rounds` rounds: first to a majority (13 -> 7)."""
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        return 0
    return rounds // 2 + 1 if rounds > 0 else 0


def max_wins_for_match(match):
    """The best-of cap that applies to one HeadToHeadMatch, or None when nothing is configured.

    Called by head_to_head.report_result: with a room configured for 13 rounds, a set cannot be
    reported 9-2, because the seventh win ends it. With NO room configured anywhere the answer is
    None and the old flat cap of 99 stands, so events that predate room settings keep working
    exactly as they did (owner rule: a new feature must not invalidate results already entered).
    """
    config, _source = resolve_for_match(match)
    if config is None:
        return None
    return wins_needed(config.rounds)


# ── resolution ───────────────────────────────────────────────────────────────────────────────
def _first_existing(candidates):
    """Return (config, scope) for the first candidate that has a configuration, else (None, None).
    candidates: [(scope, queryset filter kwargs), ...] narrowest first."""
    for scope, kwargs in candidates:
        config = CSRoomConfig.objects.filter(**kwargs).first()
        if config is not None:
            return config, scope
    return None, None


def resolve_for_match(match):
    """The configuration that applies to one Clash Squad set, narrowest first.

    match -> GROUP -> stage -> event. The group rung became real on 2026-08-13, when a Clash Squad
    stage gained the ability to be split into several group brackets: each group can carry its own
    room, so "Group A plays 13 rounds on Kalahari, Group B plays 7 on Nexterra" is one row each
    rather than a copy per match. A match whose group has no configuration keeps inheriting from
    the stage exactly as before.

    Returns (config, source_scope) where source_scope is one of SCOPES, or (None, None) when the
    event has no room settings at all. source_scope is what lets the UI say "inherited from
    Stage 1" instead of pretending every match was configured by hand.
    """
    if match is None:
        return None, None
    candidates = [("match", {"h2h_match": match})]
    if match.group_id:
        candidates.append(("group", {"group_id": match.group_id}))
    candidates += [
        ("stage", {"stage_id": match.stage_id}),
        ("event", {"event_id": match.stage.event_id}),
    ]
    return _first_existing(candidates)


def resolve_for_stage(stage):
    """The configuration that applies to a stage as a whole (stage, else its event)."""
    if stage is None:
        return None, None
    return _first_existing([
        ("stage", {"stage": stage}),
        ("event", {"event_id": stage.event_id}),
    ])


def resolve_for_group(group):
    """The configuration that applies to a Battle Royale group (group, else stage, else event).
    Unused by Clash Squad today - a bracket has no groups - and here so the resolver is complete
    when room settings are widened to BR lobbies."""
    if group is None:
        return None, None
    return _first_existing([
        ("group", {"group": group}),
        ("stage", {"stage_id": group.stage_id}),
        ("event", {"event_id": group.stage.event_id}),
    ])


# ── writes ───────────────────────────────────────────────────────────────────────────────────
def save_config(scope, scope_object, data, user=None):
    """Create or update THE configuration for one scope (idempotent: one row per scope).

    scope: one of SCOPES. scope_object: the Event / Stages / StageGroups / HeadToHeadMatch.
    data: a partial settings payload plus any of CONFIG_ONLY_FIELDS. Anything omitted keeps its
    stored value, so the editor can save one tab without wiping the other three.

    A brand-new row starts from blank_settings() rather than from empty JSON, so a config always
    carries a full store / economy / area document and no reader has to cope with half a room.
    """
    if scope not in SCOPES:
        raise RoomConfigError(f"Unknown scope '{scope}'.")

    clean = validate_settings(data)

    with transaction.atomic():
        config = CSRoomConfig.objects.filter(**{SCOPE_FIELD[scope]: scope_object}).first()
        if config is None:
            config = CSRoomConfig(**{SCOPE_FIELD[scope]: scope_object}, created_by=user)
            for field, value in blank_settings().items():
                setattr(config, field, value)

        for field, value in clean.items():
            setattr(config, field, value)

        # Room credentials + notes + publish state (config-only, never on a preset).
        if "room_id" in data:
            config.room_id = str(data["room_id"] or "")[:40]
        if "room_password" in data:
            config.room_password = str(data["room_password"] or "")[:40]
        if "notes" in data:
            config.notes = str(data["notes"] or "")
        if "is_published" in data:
            config.is_published = bool(data["is_published"])

        # Cross-field repair once everything is merged (areas vs the saved map).
        merged = _validate_merged({f: getattr(config, f) for f in SETTINGS_FIELDS})
        for field, value in merged.items():
            setattr(config, field, value)

        config.save()
    return config


def apply_preset(preset, base=None):
    """The settings dict produced by applying `preset` on top of `base` (default: a blank room).

    COPIES values - the caller then saves them onto a config. Nothing links back to the preset, so
    editing the preset later cannot rewrite an event that has already been played. preset_key is
    stamped with the preset's name purely so the UI can say what this was built from.
    """
    values = dict(base or blank_settings())
    values.update(settings_payload(preset))
    values["preset_key"] = preset.name[:40]
    return values


def apply_builtin_mode(mode_key, base=None):
    """Apply one of Free Fire's own one-tap modes (cs_room_catalogue.PRESET_MODES).

    These are PARTIAL - the in-game buttons overwrite the keys they name and leave the rest
    alone - so toggles are merged key by key rather than replaced wholesale.
    """
    mode = cat.PRESET_MODES.get(mode_key)
    if not mode:
        raise RoomConfigError(f"'{mode_key}' is not a built-in mode.")
    values = dict(base or blank_settings())
    patch = dict(mode["config"])
    toggles = patch.pop("toggles", None)
    values.update(patch)
    if toggles:
        merged = dict(values.get("toggles") or cat.default_toggles())
        merged.update(toggles)
        values["toggles"] = merged
    # Rounds may have moved, so the per-round documents have to follow or the last rounds of a
    # 13-round set would have no starting cash and no area.
    values["round_economy"] = cat.default_round_economy(values["rounds"])
    values["areas"] = cat.default_areas(values["rounds"], values["map_name"])
    values["preset_key"] = mode["label"][:40]
    return values


def sync_cs_groups(stage, group_rows, mode=None):
    """Materialise a Clash Squad stage's groups from what the event form sent.

    WHY (owner backlog item 21, 2026-08-13): the mode no longer lives in `stage_format`. A simple
    stage sends just a mode and gets ONE group; a stage the organizer split sends a row per group,
    each with its own mode. Both end up as StageGroups rows carrying `bracket_format`, so the rest
    of the system sees one shape.

    group_rows: [{"group_id"?, "group_name", "bracket_format", "playing_date"?, "playing_time"?}]
    mode: the single mode for an UNSPLIT stage; ignored when group_rows is non-empty.

    Groups the payload no longer mentions are deleted ONLY when they hold no bracket matches -
    removing a group that has been played would take its results with it, so those are kept and
    the caller can see them still there. Returns the list of live groups.

    CALLED BY: views.create_event / views.edit_event, from the `cs_groups` / `cs_bracket_format`
    keys the stage modal sends (frontend: ClashSquadPanel).
    """
    from .models import HeadToHeadMatch, StageGroups

    rows = list(group_rows or [])
    if not rows:
        # Unsplit stage: one group carrying the chosen mode. Named "Main bracket" because nothing
        # in the UI shows it - the organizer picked a mode, not a group.
        rows = [{"group_name": "Main bracket", "bracket_format": mode or "single_elim"}]

    valid_modes = {code for code, _label in StageGroups.BRACKET_FORMAT_CHOICES}
    kept_ids, live = [], []

    for index, row in enumerate(rows):
        fmt = str(row.get("bracket_format") or mode or "single_elim")
        if fmt not in valid_modes:
            raise RoomConfigError(f"'{fmt}' is not a Clash Squad mode.")
        name = str(row.get("group_name") or f"Group {chr(65 + index)}")[:50]

        group = None
        if row.get("group_id"):
            group = StageGroups.objects.filter(
                stage=stage, group_id=row["group_id"]).first()
        if group is None:
            # Reuse an existing group of the same name before creating a second one, so saving
            # the form twice does not double the list.
            group = StageGroups.objects.filter(stage=stage, group_name=name).first()

        if group is None:
            group = StageGroups.objects.create(
                stage=stage,
                group_name=name,
                playing_date=row.get("playing_date") or stage.start_date,
                playing_time=row.get("playing_time") or "00:00",
                teams_qualifying=stage.teams_qualifying_from_stage or 1,
                match_count=0,     # Battle Royale lobby fields; a bracket has no use for them
                match_maps=[],
            )
        group.group_name = name
        group.bracket_format = fmt
        group.group_order = index
        group.is_synthetic = False
        if row.get("playing_date"):
            group.playing_date = row["playing_date"]
        if row.get("playing_time"):
            group.playing_time = row["playing_time"]
        group.save()
        kept_ids.append(group.group_id)
        live.append(group)

    # Drop groups the organizer removed - but never one that already holds matches.
    for stale in StageGroups.objects.filter(stage=stage).exclude(group_id__in=kept_ids):
        if HeadToHeadMatch.objects.filter(group=stale).exists():
            continue
        stale.delete()

    return live


def visible_presets(user, organization_ids=()):
    """Presets `user` may apply: every AFC-global one, plus those owned by their organizations.

    organization_ids is passed in by the view (which already resolved the user's memberships)
    rather than being queried here, so this module never imports afc_organizers.
    """
    from django.db.models import Q
    query = Q(organization__isnull=True)
    if organization_ids:
        query |= Q(organization_id__in=list(organization_ids))
    return CSRoomPreset.objects.filter(query).order_by("-is_builtin", "name")
