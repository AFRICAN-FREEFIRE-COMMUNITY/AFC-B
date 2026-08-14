"""
afc_tournament_and_scrims.cs_room_views - the CLASH-SQUAD ROOM SETTINGS endpoints
(owner 2026-08-12; spec WEBSITE/tasks/cs-room-settings-spec.md, logic in cs_room.py, option
lists in cs_room_catalogue.py).

Kept in its own module the same way head_to_head_views.py / event_links.py are: feature
endpoints beside their logic sibling, out of the 19k-line views.py.

ENDPOINTS (the app is mounted at events/, see urls.py)

    GET  events/cs-room-catalogue/                         cs_room_catalogue_view
        PUBLIC. Every option list, the default store with Free Fire's prices, the map/area
        table and the six built-in modes. The editor and the player-facing card both draw
        their labels from this, so no option list is duplicated in TypeScript.
        200 -> cs_room_catalogue.catalogue_payload()

    GET  events/cs-room-settings/<scope>/<object_id>/       get_room_settings
        scope: event | stage | group | match.
        PUBLIC read, but a configuration that is not published yet comes back with the room ID
        and password blanked, and a manager sees them in full. Returns BOTH the row attached to
        this exact scope (may be null) and the RESOLVED one that actually applies, with the
        scope it came from, so the UI can say "inherited from Stage 1".
        200 -> {"scope", "object_id", "own": <config|null>, "effective": <config|null>,
                "effective_scope": "match"|"group"|"stage"|"event"|null, "can_manage": bool}

    PUT  events/cs-room-settings/<scope>/<object_id>/       save_room_settings
        Create or update THE configuration for that scope (idempotent - one row per scope).
        Body: any subset of the settings fields plus room_id / room_password / notes /
        is_published. Omitted fields keep their stored value, so one tab can save alone.
        Auth: AFC event admin OR org member with can_edit_events (the owner's decision: the
        same gate as editing the event, no new permission to explain).
        200 -> the same shape as the GET.

    DELETE events/cs-room-settings/<scope>/<object_id>/     delete_room_settings
        Remove the override at this scope so it inherits again. Same auth as PUT.
        200 -> {"message"}

    GET  events/cs-room-presets/                            list_room_presets
        Presets the caller may apply: AFC-global (the six Free Fire modes) plus those owned by
        organizations they are an active member of. Auth required.
    POST events/cs-room-presets/                            create_room_preset
        Save a configuration as a reusable preset. Body: {"name", "description",
        "organization_id" (optional - omit for a personal/AFC-global one, admins only), plus
        either "from" {"scope", "object_id"} to copy an existing config or the settings inline}.
    DELETE events/cs-room-presets/<preset_id>/              delete_room_preset
        Built-ins are never deletable.

CONSUMED BY: frontend lib/csRoom.ts -> components/cs-room-settings.tsx (the four-tab editor,
opened from the Clash Squad bracket card for a stage and from a single match) and
components/cs-room-card.tsx (the read-only card on the public event page and under a match).
"""
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.models import OrganizationMember
from afc_organizers.permissions import org_can, org_can_event

from . import cs_room
from . import cs_room_catalogue as cat
from . import h2h_notifications
from .models import (
    CSRoomConfig,
    CSRoomPreset,
    Event,
    HeadToHeadMatch,
    StageGroups,
    Stages,
)


# ── auth helpers (local copies, the event_links.py / head_to_head_views.py idiom) ────────────
def _auth_user(request):
    """Resolve the Bearer token to a user. Returns (user, None) or (None, error Response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _optional_user(request):
    """The caller when they sent a valid token, else None. Used by the PUBLIC read so a manager
    looking at the same page sees the room credentials while a spectator does not."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ")[1])


def _is_event_admin(user):
    """AFC event admin - same rule as head_to_head_views._is_event_admin."""
    if not user:
        return False
    if user.role in ("admin", "moderator", "support"):
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin", "event_admin")).exists()


def _can_manage(user, event):
    """Who may write room settings: AFC event admins, or org members with can_edit_events on the
    event's organization. The owner's decision was explicitly "the same gate as editing the
    event" - no new permission to grant, explain or forget."""
    if not user:
        return False
    return _is_event_admin(user) or org_can_event(user, "can_edit_events", event)


# ── scope resolution ─────────────────────────────────────────────────────────────────────────
def _resolve_scope(scope, object_id):
    """Turn (scope, id) from the URL into the object it names and the event that owns it.

    Returns (scope_object, event). Raises Http404 through get_object_or_404 for an unknown id and
    returns (None, None) for an unknown scope name, which the view turns into a 400.
    """
    if scope == "event":
        event = get_object_or_404(Event, event_id=object_id)
        return event, event
    if scope == "stage":
        stage = get_object_or_404(Stages.objects.select_related("event"), stage_id=object_id)
        return stage, stage.event
    if scope == "group":
        group = get_object_or_404(
            StageGroups.objects.select_related("stage__event"), group_id=object_id)
        return group, group.stage.event
    if scope == "match":
        match = get_object_or_404(
            HeadToHeadMatch.objects.select_related("stage__event"), h2h_match_id=object_id)
        return match, match.stage.event
    return None, None


def _resolve_effective(scope, scope_object):
    """The configuration that actually applies at this scope, and where it came from."""
    if scope == "match":
        return cs_room.resolve_for_match(scope_object)
    if scope == "group":
        return cs_room.resolve_for_group(scope_object)
    if scope == "stage":
        return cs_room.resolve_for_stage(scope_object)
    config = CSRoomConfig.objects.filter(event=scope_object).first()
    return (config, "event") if config else (None, None)


def _settings_response(scope, object_id, scope_object, can_manage):
    """The shared body of the GET and the PUT: this scope's own row, the effective one, and the
    scope the effective one came from.

    Room credentials are included when the caller can manage the event, or when the effective
    configuration has been published. That is the whole point of is_published: a room ID on a
    public page hours before the match is an invitation for strangers to walk in.
    """
    own = CSRoomConfig.objects.filter(
        **{cs_room.SCOPE_FIELD[scope]: scope_object}).first()
    effective, effective_scope = _resolve_effective(scope, scope_object)
    show_credentials = bool(can_manage or (effective and effective.is_published))
    return {
        "scope": scope,
        "object_id": int(object_id),
        "own": cs_room.config_payload(own, include_room_credentials=bool(can_manage)),
        "effective": cs_room.config_payload(
            effective, include_room_credentials=show_credentials),
        "effective_scope": effective_scope,
        "summary": cs_room.summary(effective),
        "can_manage": bool(can_manage),
    }


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def cs_room_catalogue_view(request):
    """GET events/cs-room-catalogue/ - every option the Free Fire room screen offers.

    PUBLIC: it is static reference data (no event, no user, nothing private) and BOTH the admin
    editor and the player-facing card need the labels. Serving it here rather than duplicating
    the lists in TypeScript means a Garena patch is one Python edit.
    Response: cs_room_catalogue.catalogue_payload() - see that module's docstring.
    Consumed by: frontend lib/csRoom.ts (fetched once and cached per page).
    """
    return Response(cat.catalogue_payload(), status=200)


@api_view(["GET", "PUT", "DELETE"])
def room_settings(request, scope, object_id):
    """events/cs-room-settings/<scope>/<object_id>/ - read, write or clear one scope's room config.

    GET    : PUBLIC. Returns this scope's own row (null when it inherits), the RESOLVED config
             that applies, and where it came from. Room ID/password only when the caller manages
             the event or the config is published.
    PUT    : create or update (idempotent). Auth: AFC event admin or can_edit_events on the org.
    DELETE : drop the override at this scope so it inherits again. Same auth as PUT.

    scope is one of event | stage | group | match; object_id is that object's primary key.
    Validation failures come back 400 with cs_room.RoomConfigError's message verbatim.
    Consumed by: frontend lib/csRoom.ts (csRoomApi.get / save / clear).
    """
    scope_object, event = _resolve_scope(scope, object_id)
    if scope_object is None:
        return Response(
            {"message": "scope must be one of event, stage, group or match."}, status=400)

    if request.method == "GET":
        user = _optional_user(request)
        return Response(
            _settings_response(scope, object_id, scope_object, _can_manage(user, event)),
            status=200)

    user, err = _auth_user(request)
    if err:
        return err
    if not _can_manage(user, event):
        return Response(
            {"message": "You do not have permission to change this event's room settings."},
            status=403)

    if request.method == "DELETE":
        deleted, _ = CSRoomConfig.objects.filter(
            **{cs_room.SCOPE_FIELD[scope]: scope_object}).delete()
        return Response({
            "message": "Room settings removed; this now inherits." if deleted
                       else "There were no room settings to remove here.",
            **_settings_response(scope, object_id, scope_object, True),
        }, status=200)

    # PUT: an optional built-in mode is applied FIRST and the body's own fields then win, so
    # "apply Esports Mode, but 7 rounds" works in a single request.
    data = dict(request.data or {})
    # Was it already published before this save? Publishing is what triggers the "room is open"
    # notice, and it must fire ONCE - not on every subsequent edit of an already-live room.
    was_published = CSRoomConfig.objects.filter(
        **{cs_room.SCOPE_FIELD[scope]: scope_object}, is_published=True).exists()
    mode_key = data.pop("apply_mode", None)
    preset_id = data.pop("apply_preset_id", None)
    try:
        if mode_key:
            base = cs_room.blank_settings()
            existing = CSRoomConfig.objects.filter(
                **{cs_room.SCOPE_FIELD[scope]: scope_object}).first()
            if existing:
                base = cs_room.settings_payload(existing)
            data = {**cs_room.apply_builtin_mode(mode_key, base), **data}
        elif preset_id:
            preset = get_object_or_404(CSRoomPreset, cs_room_preset_id=preset_id)
            if preset.organization_id and not org_can(
                    user, "can_edit_events", preset.organization):
                return Response({"message": "That preset belongs to another organization."},
                                status=403)
            data = {**cs_room.apply_preset(preset), **data}
        config = cs_room.save_config(scope, scope_object, data, user=user)
    except cs_room.RoomConfigError as e:
        return Response({"message": str(e)}, status=400)

    # First publish: hand the room ID and password to everybody it applies to (owner 2026-08-12 -
    # a CS competitor was never told anything by the platform). Best-effort inside the helper.
    if config.is_published and not was_published:
        h2h_notifications.notify_room_published(config, scope, scope_object)

    return Response({
        "message": "Room settings saved.",
        **_settings_response(scope, object_id, scope_object, True),
        "saved_config_id": config.cs_room_config_id,
    }, status=200)


@api_view(["GET", "POST"])
def room_presets(request):
    """events/cs-room-presets/ - the reusable room configurations the caller may apply.

    GET  : AFC-global presets (the six Free Fire modes, seeded read-only) plus every preset owned
           by an organization the caller is an active member of.
    POST : save a configuration as a preset. Body:
             {"name", "description",
              "organization_id": int | null,      (null/omitted = an AFC-global preset; AFC
                                                   event admins only, so one organizer cannot
                                                   publish to everybody)
              "from": {"scope", "object_id"}      copy an existing config, OR
              ...settings fields inline}
    Auth: any signed-in user may READ (they only ever see their own orgs'); writing to an
    organization needs can_edit_events there, and writing a global one needs AFC admin.
    Consumed by: frontend lib/csRoom.ts (csRoomApi.listPresets / savePreset).
    """
    user, err = _auth_user(request)
    if err:
        return err

    org_ids = list(
        OrganizationMember.objects.filter(user=user, status="active")
        .values_list("organization_id", flat=True))

    if request.method == "GET":
        presets = cs_room.visible_presets(user, org_ids)
        return Response(
            {"presets": [cs_room.preset_payload(p) for p in presets]}, status=200)

    data = dict(request.data or {})
    name = str(data.get("name") or "").strip()
    if not name:
        return Response({"message": "A preset needs a name."}, status=400)

    organization_id = data.get("organization_id")
    if organization_id:
        if int(organization_id) not in org_ids and not _is_event_admin(user):
            return Response({"message": "You are not a member of that organization."}, status=403)
    elif not _is_event_admin(user):
        return Response(
            {"message": "Only AFC admins can save a preset for everyone. Choose one of your "
                        "organizations instead."}, status=403)

    # Values come either from an existing configuration ("save these settings as a preset", the
    # normal path from the editor) or inline in the body.
    source = data.get("from") or {}
    if source:
        scope_object, event = _resolve_scope(source.get("scope"), source.get("object_id"))
        if scope_object is None:
            return Response({"message": "from.scope must be event, stage, group or match."},
                            status=400)
        config = CSRoomConfig.objects.filter(
            **{cs_room.SCOPE_FIELD[source["scope"]]: scope_object}).first()
        if config is None:
            return Response(
                {"message": "There are no room settings saved at that scope to copy."}, status=400)
        values = cs_room.settings_payload(config)
    else:
        try:
            values = {**cs_room.blank_settings(), **cs_room.validate_settings(data)}
        except cs_room.RoomConfigError as e:
            return Response({"message": str(e)}, status=400)

    preset, created = CSRoomPreset.objects.update_or_create(
        organization_id=organization_id or None,
        name=name,
        defaults={
            **values,
            "description": str(data.get("description") or "")[:255],
            "is_builtin": False,     # only the seed command mints built-ins
            "created_by": user,
        },
    )
    return Response({
        "message": "Preset saved." if created else "Preset updated.",
        "preset": cs_room.preset_payload(preset),
    }, status=201 if created else 200)


@api_view(["DELETE"])
def delete_room_preset(request, preset_id):
    """DELETE events/cs-room-presets/<preset_id>/ - remove an organization's own preset.

    Built-ins (the six Free Fire modes) are never deletable: they are the starting points every
    organizer expects to find, and deleting one for everybody is not a per-organizer decision.
    Auth: can_edit_events on the owning organization, or AFC event admin.
    Consumed by: frontend lib/csRoom.ts (csRoomApi.deletePreset).
    """
    user, err = _auth_user(request)
    if err:
        return err
    preset = get_object_or_404(CSRoomPreset, cs_room_preset_id=preset_id)
    if preset.is_builtin:
        return Response({"message": "Built-in Free Fire modes cannot be deleted."}, status=400)
    if preset.organization_id:
        if not org_can(user, "can_edit_events", preset.organization) and not _is_event_admin(user):
            return Response({"message": "You do not have permission to delete that preset."},
                            status=403)
    elif not _is_event_admin(user):
        return Response({"message": "Only AFC admins can delete a shared preset."}, status=403)
    preset.delete()
    return Response({"message": "Preset deleted."}, status=200)
