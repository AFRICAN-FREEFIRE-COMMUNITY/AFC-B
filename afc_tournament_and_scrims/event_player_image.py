"""Per-event copies of a player's esport image, made when the player REMOVES it (owner 2026-10-25,
inbox #400).

WHY THIS EXISTS. Every event surface used to read the live UserProfile.esports_pic. Letting a player
remove that picture would have emptied the overlays, media ZIPs and boards of every event they are
already in. The owner's rule: events the player is already in KEEP the image, REPLACE still shows
everywhere at once, and new registrations that require an image are refused until a new one is
uploaded (that last part is the existing _missing_registration_assets gate, which reads the profile).

CALLERS
    afc_auth.views.remove_esport_image   -> snapshot_for_events(user, pic) BEFORE the profile file goes
    afc_auth.views.upload_esport_image   -> drop_copies(user): a new picture supersedes the copies
    afc_auth.models.esports_pic_url      -> event_copy(user, event) when given event=
    views.py media ZIP / per-item download, broadcast_kit.py -> pic_for_event(user, event)
DATA: EventPlayerImage (models.py), RegisteredCompetitors, TournamentTeamMember.
"""
from django.core.files.base import ContentFile

# Registration statuses that mean the player is no longer in the event.
_OUT_STATUSES = ("withdrawn", "left", "rejected", "disqualified")


def events_player_is_in(user):
    """Event ids the player is IN: a solo registration of theirs, or a roster seat on an event team.
    Roster seats count whatever the team's registration state, except a rejected seat."""
    from .models import RegisteredCompetitors, TournamentTeamMember

    ids = set(
        RegisteredCompetitors.objects.filter(user=user)
        .exclude(status__in=_OUT_STATUSES)
        .values_list("event_id", flat=True)
    )
    ids.update(
        TournamentTeamMember.objects.filter(user=user)
        .exclude(status="rejected")
        .values_list("tournament_team__event_id", flat=True)
    )
    return {i for i in ids if i}


def snapshot_for_events(user, pic):
    """Copy `pic` (the profile's ImageFieldFile) once and point one EventPlayerImage row at the copy
    for every event the player is in. Returns the number of events kept. Nothing is written when the
    player is in no event, so a removal with no events leaves no stray file."""
    from .models import EventPlayerImage

    event_ids = events_player_is_in(user)
    if not event_ids or not pic:
        return 0
    pic.open("rb")
    try:
        raw = pic.read()
    finally:
        pic.close()
    base = str(pic.name).rsplit("/", 1)[-1]
    first = EventPlayerImage(event_id=sorted(event_ids)[0], user=user)
    first.image.save(base, ContentFile(raw), save=False)
    shared_name = first.image.name
    first.save()
    for eid in sorted(event_ids)[1:]:
        # One file on disk, many rows: the rest point at the same stored name.
        EventPlayerImage.objects.update_or_create(
            event_id=eid, user=user, defaults={"image": shared_name}
        )
    # A re-removal after a replace may leave an older row for an event: update_or_create above and the
    # first row's unique key are the only writers, so no duplicates are possible.
    return len(event_ids)


def drop_copies(user):
    """Delete every copy of this player's image and its files (the player uploaded a new picture)."""
    from .models import EventPlayerImage

    rows = list(EventPlayerImage.objects.filter(user=user))
    names = {r.image.name for r in rows if r.image}
    storage = rows[0].image.storage if rows and rows[0].image else None
    EventPlayerImage.objects.filter(user=user).delete()
    if storage:
        for name in names:
            try:
                storage.delete(name)
            except Exception:
                pass  # a file already gone must never block an upload
    return len(rows)


def event_copy(user, event):
    """The kept copy (ImageFieldFile) for this player in this event, or None."""
    from .models import EventPlayerImage

    if user is None or event is None:
        return None
    row = EventPlayerImage.objects.filter(event=event, user=user).first()
    return row.image if row and row.image else None


def pic_for_event(user, event, profile=None):
    """The image an EVENT surface should show: the kept copy first, else the profile's live picture
    (ImageFieldFile) or None. `profile` may be passed when the caller already holds it."""
    copy = event_copy(user, event)
    if copy:
        return copy
    if profile is None:
        from afc_auth.models import profile_of

        profile = profile_of(user)
    pic = getattr(profile, "esports_pic", None) if profile else None
    return pic or None
