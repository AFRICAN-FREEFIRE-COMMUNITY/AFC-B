"""
afc_polls.hydration - turning a PollOption's soft link into the player or team behind it.

WHY THIS EXISTS AT ALL
    PollOption stores `linked_type` + `linked_id` and a durable `label`. Every awards surface
    (the nominee wall, the ballot, the winner band, the hall of fame) is carried by FACES, and
    resolving 140 nominee links from the browser is 140 requests on a page most people reach on a
    phone. So the payload carries the entity, resolved in TWO queries for the whole poll.

WHY THE LINK IS SOFT AND THE LABEL IS THE TRUTH
    `linked_id` is not a foreign key on purpose (see afc_polls.models.PollOption): a published
    award winner cannot vanish from the record because somebody closed their account. So every
    function here treats a missing entity as normal, returns None for the link, and leaves `label`
    to render. A 2025 winner whose account is gone still shows their name and their vote count.

THE MONOGRAM CONTRACT
    `avatar_url` resolves the ESPORT image first and the profile avatar second, and is **null**
    rather than a placeholder when neither exists. That null is load-bearing: the frontend draws a
    designed monogram from the name, so a nominee with no photo is not a degraded card, it is the
    same card with a different fill. Sending a placeholder URL here would take that decision away
    from the surface that can actually make it look right.

NAMES ARE NOT TRANSLATED
    Nothing in this module goes through afc_auth.translation. "SCARLETT", "V-ENT ESPORTS" and
    "3C SMITH" are names, and running them through machine translation is a bug that reaches
    production quietly and is very visible when it does. The same rule applies to PollOption.label
    in afc_polls.views; only `description` (the "why nominated" line) is translatable.

HOW THIS CONNECTS
    Reads afc_auth.User / UserProfile (through afc_auth.models.esports_pic_url) and afc_team.Team.
    Called by afc_polls.views._serialize_question for the public ballot and results payloads, and
    by edition_detail. Consumed by frontend/app/(user)/polls/[slug]/page.tsx and the awards
    surfaces under app/(user)/awards/.
"""
from .models import PollOption


def _absolute(request, image_field):
    """An absolute media URL, or None. `request` is optional so the helper can be used from a
    management command, where a relative URL is the best that can honestly be produced."""
    if not image_field:
        return None
    try:
        url = image_field.url
    except ValueError:
        # An ImageField whose file was removed from disk raises rather than returning a path.
        # A missing photo is the monogram case, not an error.
        return None
    return request.build_absolute_uri(url) if request is not None else url


def hydrate_options(options, request=None):
    """{option_id: linked-entity dict or None} for a whole batch of options, in two queries.

    Batched rather than per option because the NFCA content-creators ballot alone is 17 questions
    and a live awards ballot is around 140 nominees. One query per option would be 140 round trips
    inside one request.
    """
    options = list(options)
    user_ids = [
        option.linked_id for option in options
        if option.linked_type == PollOption.LINK_USER and option.linked_id
    ]
    team_ids = [
        option.linked_id for option in options
        if option.linked_type == PollOption.LINK_TEAM and option.linked_id
    ]

    users, teams = {}, {}
    if user_ids:
        from afc_auth.models import User, esports_pic_url, profile_of

        for user in User.objects.filter(user_id__in=set(user_ids)):
            profile = profile_of(user)
            users[user.pk] = {
                "type": "user",
                "id": user.pk,
                # The player's own display name. NOT the option label: an admin may have typed
                # "SCARLETT (Team X)" as the label, and the profile link should still read as the
                # account it points at.
                "display_name": user.username,
                # Esport image first, profile avatar second, null third. See the module header.
                "avatar_url": (
                    esports_pic_url(user, request)
                    or _absolute(request, getattr(profile, "profile_pic", None))
                ),
                "team_name": "",
                "team_logo_url": None,
                "profile_url": f"/players/{user.username}",
            }
        _attach_teams(users, request)

    if team_ids:
        from afc_team.models import Team

        for team in Team.objects.filter(team_id__in=set(team_ids)):
            teams[team.pk] = {
                "type": "team",
                "id": team.pk,
                "display_name": team.team_name,
                "avatar_url": _absolute(request, team.team_logo),
                "team_name": team.team_name,
                "team_logo_url": _absolute(request, team.team_logo),
                # The team route takes the NAME, not the id. Getting this wrong ships a 404 deep
                # link that looks fine in a test that asserts the same mistake, which has already
                # happened once on this project (team invites, 2026-08-08).
                "profile_url": f"/teams/{team.team_name}",
            }

    hydrated = {}
    for option in options:
        if option.linked_type == PollOption.LINK_USER:
            hydrated[option.option_id] = users.get(option.linked_id)
        elif option.linked_type == PollOption.LINK_TEAM:
            hydrated[option.option_id] = teams.get(option.linked_id)
        else:
            hydrated[option.option_id] = None
    return hydrated


def _attach_teams(users, request=None):
    """Fill in the team chip on every hydrated PLAYER, in one query.

    A nominee card shows a team chip under the name where the person has one. Best tier first, so
    somebody on two rosters is shown under their strongest team rather than whichever row came
    back first, which would flip between page loads for no reason the reader could see.
    """
    if not users:
        return
    from afc_team.models import TeamMembers

    rows = (
        TeamMembers.objects.filter(member_id__in=users.keys())
        .select_related("team")
        .order_by("team__team_tier", "id")
    )
    for row in rows:
        entry = users.get(row.member_id)
        if entry is None or entry["team_name"]:
            continue
        entry["team_name"] = row.team.team_name
        entry["team_logo_url"] = _absolute(request, row.team.team_logo)
