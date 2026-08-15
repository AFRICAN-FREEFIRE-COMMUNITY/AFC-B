"""
afc_polls.eligibility - WHY a person may or may not answer a poll, requirement by requirement.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
    "The button is greyed out" generates support tickets. A refusal has to say what is needed,
    what YOURS is, and, where one exists, how to fix it. So `check_eligibility` returns a
    PER-REQUIREMENT BREAKDOWN, never a boolean: the UI cannot explain a refusal it was never told
    the shape of. The requirements panel is then shown to EVERYONE, pass or fail, because it is
    part of the poll rather than an error state. An eligible voter seeing four green ticks
    understands what kind of poll this is.

CALLED FROM EXACTLY TWO PLACES (polls spec 2.2)
    GET  polls/<slug>/            -> returns the verdict alongside the poll, so the page can render
                                     the checklist.
    POST polls/<slug>/responses/  -> calls it AGAIN before writing anything, and 403s with the same
                                     verdict body if it fails.
    The second call is not optional. It is the only real gate; anything the client does is a
    courtesy. It also means a person who fills in their UID and resubmits is let through
    immediately, with no cache to bust.

HOW A REQUIREMENT IS DECIDED
    Each filter is probed by resolving a MINI SPEC that contains that filter and nothing else, and
    asking whether this user is in the resulting queryset. That is deliberate: the probe runs the
    same afc_auth.audience code as the real gate, so a requirement can never quietly disagree with
    the decision it is explaining. `eligible` itself is NOT the AND of the requirement rows; it is
    a separate resolve_audience call over the WHOLE spec, because the spec's blocks union in a way
    a flat list of ticks cannot express (see `match_rule` below).

    `require_profile_fields` is the exception, and the reason it is an exception is in
    afc_auth.audience.parse_audience_spec: it narrows nobody, so it is not in the queryset at all.
    It is evaluated only here, and it is the one requirement type where a refusal is EXPECTED to be
    temporary, which is why its copy says "yet".

HOW THIS CONNECTS
    Reads afc_polls.PollEligibilityRule.spec, and through afc_auth.audience.resolve_audience the
    User / Team / TeamMembers / RegisteredCompetitors / afc_rankings tables. Writes nothing.
    Consumed by afc_polls.views (poll_detail and submit_response), rendered by
    frontend/app/(user)/polls/[slug]/_components/RequirementsPanel.tsx.
"""
from afc_auth.audience import parse_audience_spec, resolve_audience, spec_is_empty

# ── the profile fields an admin may require, and how each one is explained ────────────────────
# Real columns on afc_auth.User. Each carries the sentence the voter reads and a link that fixes
# it in about fifteen seconds, which is the entire argument for decision 10.
PROFILE_FIELDS = {
    "uid": {
        "label": "In-game UID",
        "requirement_text": "Your Free Fire in-game UID must be on your profile",
        "fix_hint": "Add your UID to your profile, then come back and vote",
        "fix_url": "/profile/edit",
    },
    "country": {
        "label": "Country",
        "requirement_text": "Your country must be on your profile",
        "fix_hint": "Set your country on your profile, then come back and vote",
        "fix_url": "/profile/edit",
    },
}

# Season tier is 0-based and 0 is the BEST, the opposite way round from the hand-set team tier
# where 1 is best. The raw integer is NEVER shown to anybody; these names are what the UI reads.
SEASON_TIER_NAMES = {0: "Elite", 1: "Competitive", 2: "Rising", 3: "Entry"}

TEAM_ROLE_NAMES = {
    "team_captain": "Team captain",
    "vice_captain": "Vice captain",
    "member": "Player",
    "coach": "Coach",
    "manager": "Manager",
    "analyst": "Analyst",
}


def _requirement(key, label, requirement_text, passed, your_value="", fix_hint="", fix_url=""):
    """One line of the panel. `passed` is None for "cannot be told yet", which is what a signed-out
    visitor sees: claiming a rule fails for somebody we have not identified would be a lie, and
    claiming it passes would be worse."""
    return {
        "key": key,
        "label": label,
        "requirement_text": requirement_text,
        "passed": passed,
        "your_value": your_value,
        "fix_hint": fix_hint,
        "fix_url": fix_url,
    }


def _blank_spec():
    """A parsed spec with every block empty. The base for a single-filter probe."""
    return parse_audience_spec({})


def _probe(spec, key, user, value=None):
    """Is `user` selected by a spec containing ONLY `spec[key]`?

    One SQL EXISTS per requirement line. The alternative (re-deriving each rule by hand here) is
    how the explanation and the gate drift apart, and a poll that refuses somebody for a reason
    the panel says they satisfy is worse than no panel."""
    mini = _blank_spec()
    mini[key] = spec[key] if value is None else value
    # A probe must see the same population the real gate sees, or a suspended account would be
    # told it failed the country rule.
    mini["include_suspended"] = spec.get("include_suspended", False)
    return resolve_audience(mini).filter(pk=user.pk).exists()


# ── "what is yours", per filter ───────────────────────────────────────────────────────────────
# Each of these answers the second half of a requirement line. They are separate from the pass
# decision on purpose: the decision comes from the audience engine, and these only describe.


def _user_teams(user):
    """Every team this user is on or owns, best-tier first. The team-based requirements all read
    this, so a person on two teams is judged by their strongest one rather than by whichever row
    the database happened to return."""
    from afc_team.models import Team, TeamMembers

    team_ids = set(
        TeamMembers.objects.filter(member=user).values_list("team_id", flat=True)
    ) | set(Team.objects.filter(team_owner=user).values_list("team_id", flat=True))
    return list(Team.objects.filter(team_id__in=team_ids).order_by("team_tier"))


def _your_team_tier(teams):
    if not teams:
        return "You are not on a team"
    return ", ".join(sorted({f"Tier {team.team_tier}" for team in teams}))


def _your_season_tier(user, teams, scope):
    """The viewer's computed season tier, named and dated. The date matters: the panel has to be
    able to say WHICH quarter a frozen tier came from, or a person promoted last week cannot tell
    why they are being refused."""
    from afc_auth.audience import _quarterly_season
    from afc_rankings.models import PlayerQuarterlyScore, TeamQuarterlyScore

    season = _quarterly_season()
    if not season:
        return "No season has been scored yet"
    if scope == "player":
        row = PlayerQuarterlyScore.objects.filter(player=user, season=season).first()
        tier = row.tier_assigned if row else None
    else:
        rows = TeamQuarterlyScore.objects.filter(
            team__in=[team.team_id for team in teams], season=season, tier_assigned__isnull=False
        ).order_by("tier_assigned")
        tier = rows[0].tier_assigned if rows else None
    if tier is None:
        return f"Not tiered in {season.name}"
    return f"{SEASON_TIER_NAMES.get(tier, tier)}, from {season.name}"


def _your_rank(user, teams, scope):
    from afc_auth.audience import _quarterly_season
    from afc_rankings.models import PlayerQuarterlyScore, TeamQuarterlyScore

    season = _quarterly_season()
    if not season:
        return "No season has been scored yet"
    if scope == "player":
        row = PlayerQuarterlyScore.objects.filter(
            player=user, season=season, rank__isnull=False
        ).order_by("rank").first()
    else:
        row = TeamQuarterlyScore.objects.filter(
            team__in=[team.team_id for team in teams], season=season, rank__isnull=False
        ).order_by("rank").first()
    if not row:
        return f"Unranked in {season.name}"
    return f"#{row.rank} in {season.name}"


def _your_team_roles(user):
    from afc_team.models import Team, TeamMembers

    roles = set(
        TeamMembers.objects.filter(member=user).values_list("management_role", flat=True)
    )
    # The direct FK representation, which can disagree with the roster row. Both count.
    if Team.objects.filter(team_captain=user).exists():
        roles.add("team_captain")
    if not roles:
        return "You are not on a team"
    return ", ".join(sorted(TEAM_ROLE_NAMES.get(role, role) for role in roles))


def _name_list(values, mapper=None):
    """Human list: "Tier 1 or Tier 2". Used for the "what is needed" half of every line."""
    names = [str(mapper(v)) if mapper else str(v) for v in values]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


# ── the verdict ───────────────────────────────────────────────────────────────────────────────


def check_eligibility(poll, user):
    """Decide whether `user` may answer `poll`, and explain every requirement either way.

    Returns:
        {
          "eligible": bool,
          "match_rule": "all" | "any",   # how the requirement rows combine, see below
          "requirements": [ {key, label, requirement_text, passed, your_value,
                             fix_hint, fix_url}, ... ],
          "snapshot": {...}              # what to stamp on PollResponse.eligibility_snapshot
        }

    `match_rule` exists because the spec's blocks do not all combine the same way. The category
    filters (country, tier, role, event, rank, ...) INTERSECT, so they read as "you must meet all
    of these". But an explicitly PICKED player or team UNIONS with them, so once a poll has picked
    people, meeting any one group is enough. The panel has to word its heading differently in the
    two cases, and it cannot work that out from a flat list of ticks.
    """
    rule = getattr(poll, "eligibility", None)
    spec = parse_audience_spec(rule.spec if rule and rule.spec else {})

    requirements = []
    signed_in = bool(user and getattr(user, "is_authenticated", False))

    # Decision 4: login is always required. It is the first line of the panel, and for a signed-out
    # visitor it is the ONLY line that can be decided, so every other line is left undecided
    # (passed=None) rather than guessed at.
    requirements.append(_requirement(
        key="signed_in",
        label="AFC account",
        requirement_text="You need to be signed in to AFC",
        passed=True if signed_in else False,
        your_value="Signed in" if signed_in else "Not signed in",
        fix_hint="" if signed_in else "Sign in, or create an account",
        fix_url="" if signed_in else "/login",
    ))

    # An empty spec means the poll is open to everyone with an account. Say so as a passing line
    # rather than showing an empty panel, so the voter learns what kind of poll this is.
    if spec_is_empty(spec) and not spec["require_profile_fields"]:
        requirements.append(_requirement(
            key="everyone",
            label="Who can vote",
            requirement_text="Anyone with an AFC account can vote in this poll",
            passed=True if signed_in else None,
            your_value="",
        ))
        return {
            "eligible": signed_in,
            "match_rule": "all",
            "requirements": requirements,
            "snapshot": _snapshot(poll, user) if signed_in else {},
        }

    if spec["everyone"]:
        requirements.append(_requirement(
            key="everyone", label="Who can vote",
            requirement_text="Anyone with an AFC account can vote in this poll",
            passed=True if signed_in else None,
        ))

    teams = _user_teams(user) if signed_in else []

    # ── the picked block (unions with the categories) ────────────────────────────────────────
    picked = bool(spec["user_ids"] or spec["team_ids"])
    if picked:
        passed = _probe(spec, "user_ids", user) or _probe(spec, "team_ids", user) if signed_in else None
        requirements.append(_requirement(
            key="invited",
            label="Invited",
            requirement_text="You or your team were picked for this poll",
            passed=passed,
            your_value="You were picked" if passed else ("Not picked" if signed_in else ""),
        ))

    # ── the category block (these intersect with each other) ─────────────────────────────────
    if spec["countries"]:
        requirements.append(_requirement(
            key="countries",
            label="Country",
            requirement_text=f"You must be in {_name_list(spec['countries'])}",
            passed=_probe(spec, "countries", user) if signed_in else None,
            your_value=(user.country or user.ip_country or "Not set") if signed_in else "",
            # A country rule you fail is not a rule you can fix, and pretending otherwise would be
            # worse than saying nothing. The only fix offered is for a country that is BLANK.
            fix_hint="Set your country on your profile" if signed_in and not (user.country or user.ip_country) else "",
            fix_url="/profile/edit" if signed_in and not (user.country or user.ip_country) else "",
        ))

    if spec["roles"]:
        requirements.append(_requirement(
            key="roles",
            label="Account role",
            requirement_text=f"Your AFC account must be {_name_list(spec['roles'])}",
            passed=_probe(spec, "roles", user) if signed_in else None,
            your_value=(user.role or "") if signed_in else "",
        ))

    if spec["languages"]:
        requirements.append(_requirement(
            key="languages",
            label="Language",
            requirement_text=f"Your language must be {_name_list(spec['languages'])}",
            passed=_probe(spec, "languages", user) if signed_in else None,
            your_value=(user.language or "en") if signed_in else "",
        ))

    if spec["tiers"]:
        requirements.append(_requirement(
            key="tiers",
            label="Team tier",
            requirement_text=f"Your team must be {_name_list(spec['tiers'], lambda t: f'Tier {t}')}",
            passed=_probe(spec, "tiers", user) if signed_in else None,
            your_value=_your_team_tier(teams) if signed_in else "",
            fix_hint="See how tiers are set" if signed_in else "",
            fix_url="/rankings" if signed_in else "",
        ))

    if spec["season_tiers"]:
        block = spec["season_tiers"]
        scope_word = "your own" if block["scope"] == "player" else "your team's"
        requirements.append(_requirement(
            key="season_tiers",
            label="Season tier",
            requirement_text=(
                f"{scope_word.capitalize()} season tier must be "
                f"{_name_list(block['values'], lambda v: SEASON_TIER_NAMES.get(v, v))}"
            ),
            passed=_probe(spec, "season_tiers", user) if signed_in else None,
            your_value=_your_season_tier(user, teams, block["scope"]) if signed_in else "",
            # Frozen at poll open, so there is nothing to fix inside this poll's lifetime. The link
            # explains how the tier is earned, which is the honest version of a fix.
            fix_hint="See how season tiers are earned" if signed_in else "",
            fix_url="/rankings" if signed_in else "",
        ))

    if spec["rank_range"]:
        block = spec["rank_range"]
        scope_word = "You" if block["scope"] == "player" else "Your team"
        requirements.append(_requirement(
            key="rank_range",
            label="Ranking",
            requirement_text=(
                f"{scope_word} must be ranked #{block['from']} to #{block['to']}"
                + (", as ranked when this poll opened" if block.get("frozen_at") else "")
            ),
            passed=_probe(spec, "rank_range", user) if signed_in else None,
            your_value=_your_rank(user, teams, block["scope"]) if signed_in else "",
            fix_hint="See the rankings" if signed_in else "",
            fix_url="/rankings" if signed_in else "",
        ))

    if spec["team_roles"]:
        requirements.append(_requirement(
            key="team_roles",
            label="Team role",
            requirement_text=(
                "You must be "
                + _name_list(spec["team_roles"], lambda r: TEAM_ROLE_NAMES.get(r, r).lower())
                + " of your team (however your team records it)"
            ),
            passed=_probe(spec, "team_roles", user) if signed_in else None,
            your_value=_your_team_roles(user) if signed_in else "",
        ))

    if spec["event_ids"]:
        from afc_tournament_and_scrims.models import Event

        names = list(
            Event.objects.filter(event_id__in=spec["event_ids"])
            .values_list("event_name", flat=True)
        ) or [f"event {eid}" for eid in spec["event_ids"]]
        passed = _probe(spec, "event_ids", user) if signed_in else None
        requirements.append(_requirement(
            key="event_ids",
            label="Event",
            requirement_text=f"You or your team must be registered in {_name_list(names)}",
            passed=passed,
            your_value=("Registered" if passed else "Not registered") if signed_in else "",
        ))

    # ── the fixable block: profile fields (decision 10) ──────────────────────────────────────
    # Never in the queryset. A person with an empty UID IS the audience, they simply cannot vote
    # yet, and this is the one requirement whose refusal is expected to be temporary.
    profile_ok = True
    for field in spec["require_profile_fields"]:
        meta = PROFILE_FIELDS.get(field)
        if not meta:
            continue
        value = (getattr(user, field, "") or "").strip() if signed_in else ""
        field_ok = bool(value)
        profile_ok = profile_ok and (field_ok or not signed_in)
        requirements.append(_requirement(
            key=f"profile_{field}",
            label=meta["label"],
            requirement_text=meta["requirement_text"],
            passed=field_ok if signed_in else None,
            your_value=(value or "Not set yet") if signed_in else "",
            fix_hint="" if field_ok else meta["fix_hint"],
            fix_url="" if field_ok else meta["fix_url"],
        ))

    # ── the authoritative decision ───────────────────────────────────────────────────────────
    # NOT the AND of the rows above. One resolve_audience call over the WHOLE spec, which is the
    # same query the submit path runs, so the panel can never authorise something the gate refuses.
    if not signed_in:
        audience_ok = False
    elif spec_is_empty(spec):
        audience_ok = True          # only profile requirements were set; everyone is in scope
    else:
        audience_ok = resolve_audience(spec).filter(pk=user.pk).exists()

    return {
        "eligible": bool(signed_in and audience_ok and profile_ok),
        "match_rule": "any" if picked else "all",
        "requirements": requirements,
        "snapshot": _snapshot(poll, user) if signed_in else {},
    }


def _snapshot(poll, user):
    """What to stamp on PollResponse.eligibility_snapshot: the verdict's inputs AS THEY WERE.

    Without this a result set is indefensible six weeks later, when half the voters have changed
    tier and nobody can say what the rule actually selected on the day.

    ON AN ANONYMOUS POLL this is stripped to BUCKET VALUES ONLY, never ids: a snapshot carrying a
    team id and a role is a name, and storing one beside an answer would undo the whole point of
    afc_polls.models.PollResponse (spec 1.7).
    """
    teams = _user_teams(user)
    bucket = {
        "country": user.country or user.ip_country or "",
        "role": user.role or "",
        "team_tier": sorted({team.team_tier for team in teams}),
    }
    if poll.anonymous:
        return bucket
    return {
        **bucket,
        "user_id": user.pk,
        "team_ids": [team.team_id for team in teams],
        "language": user.language or "en",
    }
