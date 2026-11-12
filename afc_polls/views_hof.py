"""The awards hall of fame (owner, 2 Nov 2026, inbox #445; round 5 mockup approved 11 Nov 2026, inbox #537).

GET polls/hall-of-fame/  - PUBLIC (no sign-in). Every PUBLISHED winner of every edition whose winners are public, newest
season first, plus who has won most. An edition hidden until release (#543) is left out entirely, for everybody.

Response: {
  "seasons": [{"slug", "title", "year", "theme",
               "winners": [{"award", "award_slug", "winner", "winner_slug", "image", "linked_type", "linked_id"}]}],
  "most_decorated": [{"name", "wins", "seasons": [year...], "image"}]   (two wins or more, most first, up to 10)
}
A person is the same across seasons when the option links the same account (linked_type + linked_id), else by the
label without case or spaces. The picture is the option's own (image_url) or the linked account's
(afc_polls.hydration.hydrate_options). No vote counts anywhere: the hall of fame names winners, it does not rank votes.

CONSUMED BY: frontend app/(user)/awards/_kol/KolHallOfFame.tsx (the winners page of a themed edition).
CONNECTS TO: models.AwardsEdition (winners_are_public, hidden), PollQuestion.published_winner_option.
"""

import re
from collections import defaultdict

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .hydration import hydrate_options
from .models import AwardsEdition, PollQuestion


def _person_key(option):
    if option.linked_id and option.linked_type and option.linked_type != "none":
        return "%s:%s" % (option.linked_type, option.linked_id)
    return "label:" + re.sub(r"[^0-9a-z]+", "", (option.label or "").casefold())


def _picture(option, hydrated):
    if option.image_url:
        return option.image_url
    linked = hydrated.get(option.option_id) or {}
    return linked.get("avatar_url") or linked.get("team_logo_url") or ""


@api_view(["GET"])
def hall_of_fame(request):
    """See the module docstring."""
    editions = [e for e in AwardsEdition.objects.filter(hidden=False).order_by("-year", "-order", "-edition_id")
                if e.winners_are_public()]
    questions = list(PollQuestion.objects.filter(poll__edition__in=editions, published_winner_option__isnull=False)
                     .select_related("published_winner_option", "poll").order_by("poll_id", "order", "question_id"))
    hydrated = hydrate_options([q.published_winner_option for q in questions], request)
    by_edition = defaultdict(list)
    tally = {}
    for q in questions:
        o = q.published_winner_option
        image = _picture(o, hydrated)
        by_edition[q.poll.edition_id].append({
            "award": q.prompt, "award_slug": q.slug, "winner": o.label, "winner_slug": o.slug, "image": image,
            "linked_type": o.linked_type, "linked_id": o.linked_id,
        })
        key = _person_key(o)
        row = tally.setdefault(key, {"name": o.label, "wins": 0, "seasons": [], "image": image})
        row["wins"] += 1
        row["image"] = row["image"] or image
        year = next((e.year for e in editions if e.edition_id == q.poll.edition_id), None)
        if year and year not in row["seasons"]:
            row["seasons"].append(year)
    seasons = [{"slug": e.slug, "title": e.title, "year": e.year, "theme": e.theme, "winners": by_edition[e.edition_id]}
               for e in editions if by_edition[e.edition_id]]
    decorated = sorted((r for r in tally.values() if r["wins"] >= 2), key=lambda r: (-r["wins"], r["name"].casefold()))
    return Response({"seasons": seasons, "most_decorated": decorated[:10]})
