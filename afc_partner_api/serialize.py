# afc_partner_api/serialize.py
# ──────────────────────────────────────────────────────────────────────────────
# The partner-facing serialization FIREWALL - the single most security-critical
# module in this app. Every read endpoint passes its ORM objects through one of the
# functions here before anything reaches the wire, so this file is the ONE boundary
# that decides what a partner can ever see.
#
# Two rules, applied to EVERY function (spec §8):
#
#   1. ALLOWLIST, not denylist. A field is emitted ONLY because this code explicitly
#      put it in the output dict. We build small dicts of public handles + dates +
#      status by hand; we NEVER `return model.__dict__` or spread a `.values()` row,
#      because that is exactly how a raw PK / room credential / PII column leaks. If a
#      field is not written here on purpose, it does not exist for the partner.
#
#   2. TOGGLE GATES on stats/details. Public handles (slug, name, in-game id, dates,
#      status, placement-vs-others ordering) are always safe to emit, but every stat
#      or detail field (placements, kills, damage, assists, rosters, maps, prize, mvp)
#      is wrapped in `if partner.include_<x>:` and appears ONLY when that toggle is on.
#      Toggles default OFF (least privilege), so a brand-new partner sees handles only.
#
# What is NEVER emitted, anywhere (the test denylist enforces this):
#   • raw DB PKs            - event_id, match_id, stage_id, group_id,
#                             tournament_team_id, player_id, competitor_id,
#                             leaderboard_id, organization_id
#   • room credentials      - room_id, room_password, room_name
#   • PII / contact         - contact_email, email, full_name/real names, discord_id,
#                             discord_role_id (stage/group/waitlist discord role ids)
#   • internal config/flags - the raw scoring_settings JSON, rankings_verified, is_draft,
#                             creator, partner_published
# `is_native_afc` is derived as `organization_id is None` (a boolean), so partners
# learn an event is a native AFC event WITHOUT ever receiving the raw org PK.
#
# Added UNGATED on 2026-10-10 (owner, inbox #220 + #221), each because the public site
# already shows it to a signed-out visitor, so a partner learns nothing a spectator cannot:
#   • country + country_code on every team and player (the flag beside a name on the site;
#     _country_fields below);
#   • points on every standings row (the score the row is RANKED by: a table of ranks
#     without the score that produced them is not a leaderboard);
#   • the point system (point_system on events and matches, champion_point / point_rush on
#     stages): the rules shown on the event's Structure tab. It is built field by field from
#     Match.scoring_settings (_point_system), never passed through raw.
# The parts of a points breakdown that would reveal a GATED stat stay behind that stat's
# toggle: placement_points and booyahs behind include_placements, kill_points behind
# include_kills.
#
# Aggregation note: match/team/standings/player stats are folded from the
# ALREADY-FINALIZED stat rows (TournamentTeamMatchStats for squad/duo events,
# SoloPlayerMatchStats for solo events) - the same rows the admin standings view sums
# (afc_tournament_and_scrims.views.get_all_leaderboard_details_for_event). We reuse
# that summation but strip the result to the public, toggled-on fields only.
# Full spec: WEBSITE/tasks/partner-api-design.md (§8 serialization rules).
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.db.models import Case, Count, IntegerField, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from afc_tournament_and_scrims.models import (
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
)


# ── media urls ─────────────────────────────────────────────────────────────────
def _media_url(filefield):
    """ABSOLUTE url for an ImageField/FileField, or None when the field is empty.

    Why absolute: media lives on the AFC box's local disk (MEDIA_ROOT) and is served by
    nginx under MEDIA_URL ("/media/..."). A partner fetches these urls from its OWN
    infrastructure, so a site-relative path would resolve against the PARTNER's domain
    and 404. settings.AFC_API_BASE_URL is the public origin that fronts /media/, so we
    join the two. Same approach afc_sso.claims uses for the profile picture claim.

    Guarded on purpose: an ImageField whose file is missing/blank raises ValueError on
    .url, and every caller here is a serializer that must never 500 on absent art (spec
    §11 "field toggle on but the underlying data absent -> emit null, not an error").

    CALLERS: serialize_event (event_banner, uploaded_rules), serialize_team (team_logo),
    serialize_player (UserProfile.esports_pic), serialize_design (background art + logos).
    """
    if not filefield:
        return None
    try:
        path = filefield.url
    except ValueError:
        return None
    return f"{settings.AFC_API_BASE_URL.rstrip('/')}{path}"


# ── country ────────────────────────────────────────────────────────────────────
def _country_fields(raw):
    """{"country": "Nigeria", "country_code": "NG"} for a stored country value, both None when
    it is blank or not a country (inbox #220).

    AFC's country columns hold a mix of ISO codes and names ('NG' and 'Nigeria' are both in
    production), so the partner gets the ISO 3166-1 alpha-2 code as the key and ONE name per
    code, whatever spelling the row holds. The resolver is afc_auth.country_grouping, which
    mirrors the frontend's flag table so the API and the site agree on every value.

    WHICH column is the caller's job, and each caller uses the rule the site's own flag uses:
      team   -> Team.country (derived from the roster), or a ghost's GhostTeam.country;
      player -> User.ip_country or User.country (where the player is, profile as fallback).
    """
    from afc_auth.country_grouping import country_code, country_display_name

    code = country_code(raw)
    return {"country": country_display_name(code), "country_code": code}


def _player_country(user):
    """The player-flag rule, the same one afc_player.aggregation and the team roster read."""
    return _country_fields(user.ip_country or user.country)


# ── images on table rows ───────────────────────────────────────────────────────
# Owner, inbox #224 (2026-10-10): "does the data send out team logos and player esport images?"
# They always went out on /teams/ (logo_url) and /players/ + rosters (esports_image_url), behind
# include_media. The standings and the /results/ tree now carry them too, on every TABLE row and
# bracket side, so a partner can draw a leaderboard from one response. Map result rows stay lean.
def _team_logos(tournament_team_ids):
    """{tournament_team_id: absolute logo url or None}, one query. An imported (ghost) team has
    no AFC account and so no logo."""
    from afc_tournament_and_scrims.models import TournamentTeam

    return {tt.pk: (None if tt.is_ghost else _media_url(tt.team.team_logo))
            for tt in TournamentTeam.objects.filter(pk__in=list(tournament_team_ids))
            .select_related("team")}


def _player_image(user):
    """The player's esport (roster) photo, absolute, or None.

    It lives on UserProfile, NOT on User (bug found 2026-07-02: consumers read user.esports_pic,
    which does not exist, so images never showed), so it is resolved through canonical_profile,
    the SAME lowest-profile_id row the writers (upload_esport_image) and every other reader use.
    Duplicate UserProfile rows exist in prod, so any other row can miss an uploaded image. A
    PUBLIC promo headshot, not PII: no real name, email or discord ever crosses."""
    from afc_auth.models import canonical_profile

    profile = canonical_profile(user)
    return _media_url(profile.esports_pic) if profile else None


# ── point system ───────────────────────────────────────────────────────────────
def _number(value, default=0):
    """A stored scoring number as JSON: 1.0 -> 1, 0.5 stays 0.5, junk -> the default."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return int(n) if n.is_integer() else n


def _point_system(scoring_settings):
    """One map's point system, built FIELD BY FIELD from Match.scoring_settings (inbox #221).

    These four values are exactly what scores a map: afc_tournament_and_scrims.result_writes.
    scoring_context reads them off the match and scoring.compute_team_points applies them, so a
    partner can recompute any row it is sent. The raw JSON is never passed through: anything
    else somebody stores in it stays inside.

        {"placement_points": {"1": 12, "2": 9, ...}, "points_per_kill": 1,
         "points_per_assist": 0, "points_per_1000_damage": 0}

    placement_points is keyed by finishing place as a string (JSON object keys are strings), in
    place order; a place missing from it scores 0.
    """
    s = scoring_settings if isinstance(scoring_settings, dict) else {}
    table = {}
    raw_table = s.get("placement_points") if isinstance(s.get("placement_points"), dict) else {}
    for place, pts in raw_table.items():
        try:
            table[int(place)] = _number(pts)
        except (TypeError, ValueError):
            continue
    return {
        "placement_points": {str(place): table[place] for place in sorted(table)},
        "points_per_kill": _number(s.get("kill_point", 1), 1),
        "points_per_assist": _number(s.get("points_per_assist", 0)),
        "points_per_1000_damage": _number(s.get("points_per_1000_damage", 0)),
    }


def _event_point_system(ev):
    """(point_system, varies) for a whole event.

    Every map carries its own point system, and an organizer can change one map's. Measured on
    production 2026-10-10: 70 of the 72 events a partner reads use one point system on every
    map, 2 do not. So the event carries that point system when every map agrees, and None with
    varies=True when they do not (each match then carries its own, see serialize_match). An
    event with no maps yet has None and varies=False.
    """
    from afc_tournament_and_scrims.models import Match

    systems = []
    for raw in Match.objects.filter(group__stage__event=ev).values_list("scoring_settings", flat=True):
        system = _point_system(raw)
        if system not in systems:
            systems.append(system)
    if len(systems) == 1:
        return systems[0], False
    return None, len(systems) > 1


# ── event ──────────────────────────────────────────────────────────────────────
def serialize_event(ev, partner):
    """Public event card: slug + display fields + dates + status. No PKs, no flags.

    `is_native_afc` is the ONLY thing we expose about ownership - derived from
    organization_id so the raw org PK never crosses the firewall.
    """
    out = {
        "slug": ev.slug,
        "name": ev.event_name,
        "competition_type": ev.competition_type,
        "participant_type": ev.participant_type,
        "tier": ev.tournament_tier,
        "status": ev.event_status,
        "start_date": ev.start_date,
        "end_date": ev.end_date,
        "is_native_afc": ev.organization_id is None,
    }
    # The point system the event's maps are scored with (inbox #221), ungated: it is the rules
    # block on the event's public Structure tab. None + varies=True when maps differ.
    out["point_system"], out["point_system_varies"] = _event_point_system(ev)
    # Where the event is decided, and whether that stage's results are in (inbox #222). Measured
    # on production 2026-10-10: events reach partners marked completed whose semi-final and final
    # were never entered (DECA CUP Season 5, FFWS Fall SSA). Their final standings then rank the
    # last stage that HAS results, and without this a partner would take that stage's leader for
    # the champion. final_stage is the stage marked as the finals, else the last one
    # (views._final_stage_for_event, the same choice final_standings makes).
    from afc_tournament_and_scrims.views import _final_stage_for_event

    final = _final_stage_for_event(ev)
    out["final_stage"] = final.stage_name if final else None
    stats = SoloPlayerMatchStats if ev.participant_type == "solo" else TournamentTeamMatchStats
    out["final_stage_has_results"] = bool(final) and stats.objects.filter(
        match__group__stage=final).exists()
    # Prize pool is a detail field, gated on include_prize.
    if partner.include_prize:
        out["prize_pool"] = ev.prizepool
    # Event ART: the banner a broadcaster puts behind the event, plus the uploaded rules
    # document (a real file, not prose). Both absolute; None when nothing was uploaded.
    if partner.include_media:
        out["banner_url"] = _media_url(ev.event_banner)
        out["rules_file_url"] = _media_url(ev.uploaded_rules)
    # Event COPY: the short rules blurb typed into the event form.
    if partner.include_text:
        out["rules_text"] = ev.event_rules or None
    return out


# ── stage ──────────────────────────────────────────────────────────────────────
def serialize_stage(stage, partner):
    """Public stage row: name + 1-based order within the event + dates + status.

    `order` is the stage's 1-based position in RUNNING order, the order the tournament page
    shows: the organizer's manual order (stage_order), then start date (inbox #222,
    2026-10-10; it used to count by stage_id, which is creation order, so a reordered event
    came out in the wrong sequence). A sequence number, never the raw stage_id.
    """
    order = [s.stage_id for s in _stages_in_running_order(stage.event)].index(stage.stage_id) + 1
    out = {
        "stage_name": stage.stage_name,
        "order": order,
        "format": stage.stage_format,
        "status": stage.stage_status,
        "start_date": stage.start_date,
        "end_date": stage.end_date,
    }
    # The stage's scoring MODES (inbox #221), part of the point system, ungated like it. Each is
    # None when the stage does not use it.
    #   champion_point: a team that reaches `threshold` points then wins a map is champion
    #                   (afc_tournament_and_scrims.scoring.champion_for_group).
    #   point_rush:     the lobby's finishing places earn `reward` bonus points, carried into the
    #                   stage named `target_stage` (scoring.rewards_from_standings). The target
    #                   is named, never its stage_id.
    out["champion_point"] = (
        {"threshold": stage.champion_point_threshold} if stage.champion_point_enabled else None)
    if stage.point_rush_enabled:
        reward = {}
        for place, pts in (stage.point_rush_reward or {}).items():
            try:
                reward[int(place)] = _number(pts)
            except (TypeError, ValueError):
                continue
        target = stage.point_rush_target_stage
        out["point_rush"] = {
            "reward": {str(place): reward[place] for place in sorted(reward)},
            "target_stage": target.stage_name if target else None,
        }
    else:
        out["point_rush"] = None
    return out


# ── group ──────────────────────────────────────────────────────────────────────
def serialize_group(group, partner):
    """Public group row: name + schedule. No PKs, no discord role id.

    Maps played in the group are a detail field, gated on include_maps.
    """
    out = {
        "group_name": group.group_name,
        "playing_date": group.playing_date,
    }
    if partner.include_maps:
        # match_maps is a plain JSON list of map names (public, no ids).
        out["maps"] = list(group.match_maps or [])
    return out


# ── match ──────────────────────────────────────────────────────────────────────
def serialize_match(match, partner):
    """Public match row: match_number + status. Room credentials are STRIPPED.

    The match carries room_id / room_password / room_name + scoring_settings, none of
    which may ever reach a partner - so we hand-pick only match_number and the public
    result flag, and gate map (include_maps) and mvp (include_mvp) behind toggles.
    """
    out = {
        "match_number": match.match_number,
        "result_inputted": match.result_inputted,
        # This map's own point system (inbox #221), built field by field (_point_system), never
        # the raw scoring_settings JSON. Ungated, like the event's.
        "point_system": _point_system(match.scoring_settings),
    }
    if partner.include_maps:
        out["map"] = match.match_map
    if partner.include_mvp:
        # MVP is the in-game handle only (or null if none recorded - spec §11 edge case).
        out["mvp"] = match.mvp.username if match.mvp else None
    return out


# ── team participation status ──────────────────────────────────────────────────
# WHY THIS EXISTS
# The teams endpoint used to list every TournamentTeam row identically: a team that won the
# event, a team still sitting on the waitlist, a team that withdrew before the first map, and a
# team that registered and never turned up all came back as the same shape with no way to tell
# them apart. A partner building a bracket or a standings graphic therefore had no choice but to
# show teams that never competed.
#
# We ADD a field rather than filtering the list. Filtering would silently change the results of
# every partner already integrated against this endpoint (their team counts and their paging would
# both move under them); an added key is backwards compatible, and it also leaves the decision of
# what to display where it belongs, with the partner.
#
# EVERY value below is read straight off columns the database already keeps. Nothing here is
# inferred from a heuristic, and there is deliberately no "eliminated", "qualified" or "champion"
# state, because the schema cannot back those:
#   TournamentTeam.status       - "active" in good standing, else "disqualified" / "withdrawn" /
#                                 "left", or "pending". NOTE "pending" is real and deliberate but
#                                 is MISSING from the model's TEAM_STATUS choices list: the
#                                 registration view writes
#                                 `status="pending" if event.is_sponsored else "active"`
#                                 (afc_tournament_and_scrims.views), so on a SPONSORED event a
#                                 registration lands awaiting approval. Django choices are
#                                 validation-only and never constrained the column, which is why
#                                 the clone really holds 9 such rows. Reporting them as
#                                 "registered" would tell a partner they were accepted.
#   TournamentTeam.is_waitlisted- registered but holding a waitlist slot, not a playing slot
#   TournamentTeam.is_no_show   - an active team the organizer marked absent (owner 2026-06-17),
#                                 which frees its slot for a waitlisted team
#   TournamentTeamMatchStats.played - per match. A row with played=False is a team that was SEEDED
#                                 into a match and did not turn up for it; the scoring code zeroes
#                                 its placement points (afc_tournament_and_scrims.scoring). So
#                                 "did this team ever actually play" is "does it have at least one
#                                 stat row with played=True", not merely "does it have stat rows".
#
# PRECEDENCE, most specific first. A team that played two maps and then withdrew reports
# "withdrawn", because for a partner the fact that it is out of the competition matters more than
# the fact that it once played: the standing it left behind is stale either way.
TEAM_STATUS_PRECEDENCE = ("disqualified", "withdrawn", "left", "pending", "waitlisted", "no_show",
                          "played", "registered")

# The status column values that ARE the answer on their own, in precedence order. Anything else
# (today only "active") falls through to the derived states below.
_TERMINAL_TEAM_STATUSES = ("disqualified", "withdrawn", "left", "pending")


def team_status(tt, played_match_count):
    """The partner-facing participation status of ONE tournament team.

    `tt` is a TournamentTeam; `played_match_count` is how many of its TournamentTeamMatchStats
    rows have played=True (serialize_team folds that into the aggregate it already runs, so this
    costs no extra query). Returns one of TEAM_STATUS_PRECEDENCE and nothing else.

    The returned set is CLOSED on purpose. TournamentTeam.status is an unconstrained CharField, so
    a value nobody planned for can appear in it (that is exactly how "pending" got there); passing
    such a value straight through would leak an unbounded vocabulary into a public API that
    partners have to switch on. An unrecognised status therefore degrades into the derived states
    rather than inventing a new contract value.

    Documented for partners in backend/PARTNER_API.md ("Team participation status") and on the
    public guide page (frontend app/(root)/partners/api). The values
    are part of the public API contract: renaming one breaks every integration reading it, so add
    a new value rather than repurposing an existing one.
    """
    # 1. Explicit states recorded on the row itself, which outrank everything derived below.
    if tt.status in _TERMINAL_TEAM_STATUSES:
        return tt.status
    # 2. Holding a waitlist slot rather than a playing slot. Checked before the stat rows because
    #    a waitlisted team can be seeded into a lobby without ever being promoted.
    if tt.is_waitlisted:
        return "waitlisted"
    # 3. Active, expected, and marked absent by the organizer.
    if tt.is_no_show:
        return "no_show"
    # 4. Actually turned up for at least one match. This is the distinction the whole field exists
    #    for, and it is the SAME signal the rest of this serializer already trusts: a team with no
    #    played rows is exactly the team whose roster comes back empty and whose stats come back
    #    zero, because _team_players reads the stat rows too.
    if played_match_count:
        return "played"
    # 5. Accepted into the event and has not played a match (yet, or ever). Also the safe landing
    #    spot for an unrecognised status column value (see docstring).
    return "registered"


# ── team ───────────────────────────────────────────────────────────────────────
def serialize_team(tt, partner):
    """One tournament team's public identity + its event-wide aggregated stats.

    `tt` is a TournamentTeam (a team's entry in one event). We fold ALL of that team's
    finalized TournamentTeamMatchStats rows across the event into a single summary, and
    emit each stat ONLY when its toggle is on:
      • include_placements -> best (lowest) placement the team achieved
      • include_kills/damage/assists -> summed across the team's matches
      • include_rosters -> the team's player list (public handles only)
    The team name/tag are always-safe public handles; no team_id / tournament_team_id.

    `status` is emitted UNGATED, alongside the handles, for the same reason they are: it is
    structural identity, not a statistic. It also carries no information a spectator could not
    read off the public event page. Putting it behind a toggle would have left every partner
    already integrated exactly as unable to tell a team that played from one that never turned
    up, which is the whole problem it exists to solve. See team_status above.
    """
    # GHOST GUARD (owner 2026-08-20, external results import): a ghost competitor has no
    # AFC account, so it carries no tag, logo, or self-written description - display_name is
    # the only honest field. tt.team is None on a ghost row, so team_tag/logo_url/description
    # fall back to the honest empty value instead of crashing on tt.team.team_tag etc.
    out = {"team": tt.display_name, "team_tag": (None if tt.is_ghost else tt.team.team_tag)}
    # The team's country (inbox #220): competitor.country is Team.country, or GhostTeam.country
    # for an imported team, the same value the site draws the team's flag from. Ungated identity.
    out.update(_country_fields(tt.competitor.country if tt.competitor else None))

    # Team BRAND art: the logo a broadcaster puts next to the team's name. Absolute url,
    # None when the team never uploaded one (or the competitor is a ghost).
    if partner.include_media:
        out["logo_url"] = None if tt.is_ghost else _media_url(tt.team.team_logo)
    # Team COPY: the short self-description shown on the team's site profile.
    if partner.include_text:
        out["description"] = None if tt.is_ghost else (tt.team.team_description or None)

    # Aggregate this team's finalized per-match stat rows once (avoids N queries below).
    # played_matches rides along in the SAME aggregate (a filtered Count, so it costs no extra
    # query and leaves every other total folded over ALL rows exactly as before) and feeds
    # team_status: it counts only the matches the team actually turned up for.
    agg = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team=tt)
        .aggregate(
            # Over the maps it PLAYED: a map a team sat out is stored with placement 0
            # (result_writes), which made "best" 0 for any team that missed one (fixed 2026-10-10).
            best_placement=Min("placement", filter=Q(placement__gt=0)),
            kills=Sum("kills"),
            damage=Sum("damage"),
            assists=Sum("assists"),
            played_matches=Count("pk", filter=Q(played=True)),
        )
    )

    # Ungated, next to the handles (see docstring). Derived from columns only, never guessed.
    out["status"] = team_status(tt, agg["played_matches"] or 0)

    if partner.include_placements:
        # Best result the team reached; null if it never recorded a match.
        out["placement"] = agg["best_placement"]
    if partner.include_kills:
        out["kills"] = agg["kills"] or 0
    if partner.include_damage:
        out["damage"] = agg["damage"] or 0
    if partner.include_assists:
        out["assists"] = agg["assists"] or 0
    if partner.include_rosters:
        # Public handles only - username + in-game id, never name/email/discord.
        # Pass `tt` so each roster player's stats are folded ONLY from this team's rows
        # in THIS event (scoped), not the player's lifetime stats across every event.
        out["roster"] = [serialize_player(p, partner, tournament_team=tt) for p in _team_players(tt)]
    return out


def _team_players(tt):
    """Distinct Users who recorded player stats for this tournament team, in a stable
    order. We read the roster from the finalized stat rows (TournamentPlayerMatchStats)
    rather than the registration tables so it reflects who actually played."""
    from afc_auth.models import User

    player_ids = (
        TournamentPlayerMatchStats.objects
        .filter(team_stats__tournament_team=tt)
        .values_list("player_id", flat=True)
        .distinct()
    )
    # order_by username for a deterministic, handle-sorted roster.
    return User.objects.filter(pk__in=list(player_ids)).order_by("username")


# ── player ─────────────────────────────────────────────────────────────────────
def serialize_player(user, partner, tournament_team=None):
    """One player's PUBLIC handle (+ optional folded stats). NEVER full_name/email/discord.

    Always emits the in-game username + in-game id (uid). Stats are folded from the
    player's finalized TournamentPlayerMatchStats rows and gated per toggle.

    `tournament_team` SCOPES the stat fold to a single team-in-one-event. It MUST be
    passed for any per-event payload (rosters, the per-event players endpoint): a
    TournamentPlayerMatchStats row links to its team via team_stats.tournament_team,
    and a tournament_team belongs to exactly one Event - so filtering on it confines
    the aggregate to this player's stats IN THIS EVENT. Without it the aggregate spans
    every event the player ever played (lifetime totals), which would leak wrong,
    cross-event numbers into a per-event response. (Left optional only for a future
    truly-global player view; every current caller passes the team.)
    """
    out = {"username": user.username, "in_game_id": user.uid}
    # The player's country (inbox #220), by the site's player-flag rule. Ungated identity.
    out.update(_player_country(user))

    # Player ESPORT IMAGE: the posed roster photo broadcasters use in lower-thirds and
    # versus cards. It lives on UserProfile, NOT on User (bug found 2026-07-02: consumers
    # read user.esports_pic, which does not exist, so images never showed) - so we resolve
    # the profile through canonical_profile, the SAME lowest-profile_id row the writers
    # (upload_esport_image) and every other reader use. Duplicate UserProfile rows exist in
    # prod, so resolving any other row can miss an image that was really uploaded.
    # This is a PUBLIC promo headshot, not PII: no real name, email or discord ever crosses.
    if partner.include_media:
        out["esports_image_url"] = _player_image(user)

    # Only touch the stat tables if at least one stat toggle is on (avoids a needless query).
    if partner.include_kills or partner.include_damage or partner.include_assists:
        rows = TournamentPlayerMatchStats.objects.filter(player=user)
        if tournament_team is not None:
            # Scope to this team's matches in this event (see docstring).
            rows = rows.filter(team_stats__tournament_team=tournament_team)
        agg = rows.aggregate(kills=Sum("kills"), damage=Sum("damage"), assists=Sum("assists"))
        if partner.include_kills:
            out["kills"] = agg["kills"] or 0
        if partner.include_damage:
            out["damage"] = agg["damage"] or 0
        if partner.include_assists:
            out["assists"] = agg["assists"] or 0
    return out


# ── standings ──────────────────────────────────────────────────────────────────
#
# THE FINAL TABLE IS AFC'S OFFICIAL ONE (inbox #220 + #222, 2026-10-10). Squad and duo
# standings come from afc_tournament_and_scrims.final_standings.event_final_standings, the
# module behind a team page's "Final placement" and the prize payouts (owner rule
# 2026-07-14): a team is placed by the LAST stage it played, deeper stages first, and inside
# that stage by the table the site shows for it (points, configured tie-breakers, Point-Rush
# carry-over, Champion-Point pin). Every number on a row is from that stage, which the row
# names in `decided_in`.
#
# This module used to sum every map of the event into one table instead. Measured on
# production 2026-10-10 over the 71 team events a partner reads: 11 came out in a different
# order from AFC's official placement, 4 with a different WINNER (DECA CUP Season 5 named
# NO PRESSURE, the official champion is V-ENT ESPORTS), and one merged every imported team
# into a single row named null. Its score also dropped assist and damage points.
#
# Every table is built from the site's shared aggregator
# (round_robin._aggregate_team_standings, through final_standings), so a partner's numbers
# are the site's numbers.
#
# Solo events have no official multi-stage module (the site's tables are team based), so a
# solo table is summed across the maps it covers, scored the way the admin standings view
# scores a solo lobby: placement + kill + bonus - penalty (a solo row's stored total_points
# leaves bonus and penalty out, scoring.compute_solo_points).


def serialize_standings(event, partner):
    """The event's final standings: ranked rows carrying a public handle, the country, the score
    (`points`) and the toggled stats. Never a competitor or team PK.

    Solo events rank players (username + in_game_id); squad and duo events rank teams.
    """
    if event.participant_type == "solo":
        return _solo_table(SoloPlayerMatchStats.objects.filter(match__group__stage__event=event),
                           partner)
    return _final_team_standings(event, partner)


def _team_extras(stats):
    """{tournament_team_id: {damage, assists, best_placement}} over a stats queryset: the three
    columns a partner already received that the shared aggregator does not carry. One grouped
    query. Best finish is over PLAYED maps (a map a team sat out is stored with placement 0).
    The id is a dict key for the caller and is never emitted."""
    return {
        r["tournament_team_id"]: r
        for r in stats.values("tournament_team_id").annotate(
            damage=Coalesce(Sum("damage"), 0),
            assists=Coalesce(Sum("assists"), 0),
            best_placement=Min("placement", filter=Q(placement__gt=0)),
        )
    }


def _team_row(rank, row, extras, partner, logos):
    """One public standings row from a shared-aggregator row (+ its carry-over, if folded).
    `logos` is _team_logos over the table's teams (read only when include_media is on)."""
    more = extras.get(row["tournament_team_id"], {})
    entry = {"rank": rank, "team": row["team_name"]}
    entry.update(_country_fields(row.get("team_country")))
    if partner.include_media:
        entry["logo_url"] = logos.get(row["tournament_team_id"])
    _apply_standings_fields(entry, partner, {
        # effective_total already holds any carry-over the official builders folded in.
        "points": row["effective_total"],
        "carry_over_points": row.get("carry_over_points", 0),
        "placement_points": row["placement_sum"],
        "kill_points": row["kill_sum"],
        "bonus_points": row["bonus_sum"],
        "penalty_points": row["penalty_sum"],
        "booyahs": row["total_booyah"],
        "matches_played": row["games_played"],
        "placement": more.get("best_placement"),
        "kills": row["total_kills"],
        "damage": more.get("damage", 0),
        "assists": more.get("assists", 0),
    })
    return entry


def _final_team_standings(event, partner):
    from afc_tournament_and_scrims.final_standings import (
        event_final_standings, official_stage_standings)
    from afc_tournament_and_scrims.models import Stages, TournamentTeam

    ordered, _rank_by_tt, _reached, _final = event_final_standings(event)
    logos = (_team_logos(item["tournament_team_id"] for item in ordered)
             if partner.include_media else {})
    tables = {}  # stage_id -> ({tournament_team_id: official row}, extras)
    out = []
    for item in ordered:
        stage_id = item["stage_id"]
        if stage_id not in tables:
            stage = Stages.objects.get(pk=stage_id)
            rows = {r["tournament_team_id"]: r for r in official_stage_standings(stage)}
            extras = _team_extras(TournamentTeamMatchStats.objects.filter(match__group__stage=stage))
            tables[stage_id] = (rows, extras)
        rows, extras = tables[stage_id]
        row = rows.get(item["tournament_team_id"])
        if row is None:
            # event_final_standings' own defensive branch: a team in a tier but missing from the
            # stage table. Never seen in production; a zero row keeps it visible and named.
            tt = TournamentTeam.objects.filter(pk=item["tournament_team_id"]).first()
            row = {"tournament_team_id": item["tournament_team_id"],
                   "team_name": tt.display_name if tt else None,
                   "team_country": tt.competitor.country if tt and tt.competitor else None,
                   "effective_total": 0, "placement_sum": 0, "kill_sum": 0, "bonus_sum": 0,
                   "penalty_sum": 0, "total_booyah": 0, "games_played": 0, "total_kills": 0}
        entry = _team_row(item["rank"], row, extras, partner, logos)
        # Where this place was decided, and whether the team made the event's final stage.
        # Inserted right after the identity so a row reads top to bottom.
        identity = ("rank", "team", "country", "country_code", "logo_url")
        entry = {**{k: entry[k] for k in identity if k in entry},
                 "decided_in": item["stage_name"],
                 "reached_final_stage": bool(item["reached_final_stage"]),
                 **{k: v for k, v in entry.items() if k not in identity}}
        out.append(entry)
    return out


# The solo booyah: a first place. A solo row is always one real match (the imported aggregate
# rows of the external results import are team rows only), so there is no booyah_count to read.
# Using the TEAM expression here is what answered 500 on every solo event (inbox #202).
_SOLO_BOOYAH = Sum(
    Case(
        When(placement=1, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
)

# The solo score, as the admin standings view computes it for a solo lobby.
_SOLO_POINTS = (
    Coalesce(Sum("placement_points"), 0)
    + Coalesce(Sum("kill_points"), 0)
    + Coalesce(Sum("bonus_points"), 0)
    - Coalesce(Sum("penalty_points"), 0)
)


def _solo_table(stats, partner, carry_over=None):
    """Rank the players in a SoloPlayerMatchStats queryset (a whole event, or one group).

    `carry_over` is the Point-Rush head start keyed by competitor id ({} or None for none); it is
    folded into points before ranking, exactly as the team builders fold theirs.
    """
    carry_over = carry_over or {}
    rows = list(
        stats
        # competitor_id is the carry-over key; both country columns ride along for the player-flag
        # rule (ip_country or country). All belong to one competitor, so the GROUP BY is not split.
        .values("competitor_id", "competitor__user__username", "competitor__user__uid",
                "competitor__user__ip_country", "competitor__user__country")
        .annotate(
            points=_SOLO_POINTS,
            placement_points=Coalesce(Sum("placement_points"), 0),
            kill_points=Coalesce(Sum("kill_points"), 0),
            bonus_points=Coalesce(Sum("bonus_points"), 0),
            penalty_points=Coalesce(Sum("penalty_points"), 0),
            booyahs=_SOLO_BOOYAH,
            kills=Coalesce(Sum("kills"), 0),
            best_placement=Min("placement", filter=Q(placement__gt=0)),
            matches_played=Count("id"),
        )
    )
    for r in rows:
        r["carry_over_points"] = carry_over.get(r["competitor_id"], 0)
        r["points"] += r["carry_over_points"]
    rows.sort(key=lambda r: (-r["points"], -r["booyahs"], -r["kills"],
                             r["competitor__user__username"] or ""))
    images = {}
    if partner.include_media:
        from afc_tournament_and_scrims.models import RegisteredCompetitors

        images = {rc.pk: (_player_image(rc.user) if rc.user else None)
                  for rc in RegisteredCompetitors.objects.filter(
                      pk__in=[r["competitor_id"] for r in rows]).select_related("user")}
    out = []
    for i, r in enumerate(rows, start=1):
        entry = {
            "rank": i,
            "username": r["competitor__user__username"],
            "in_game_id": r["competitor__user__uid"],
        }
        entry.update(_country_fields(r["competitor__user__ip_country"] or r["competitor__user__country"]))
        if partner.include_media:
            entry["esports_image_url"] = images.get(r["competitor_id"])
        _apply_standings_fields(entry, partner, {
            "points": r["points"],
            "carry_over_points": r["carry_over_points"],
            "placement_points": r["placement_points"],
            "kill_points": r["kill_points"],
            "bonus_points": r["bonus_points"],
            "penalty_points": r["penalty_points"],
            "booyahs": r["booyahs"],
            "matches_played": r["matches_played"],
            "placement": r["best_placement"],
            "kills": r["kills"],
        })
        out.append(entry)
    return out


# ── leaderboard designs ────────────────────────────────────────────────────────
def designs_for_event(event):
    """The OrgLeaderboardDesign rows a partner may pull for ``event``.

    A design is a branded leaderboard TEMPLATE (background art + placed logos + brand
    colours) that afc_leaderboard.graphic composites live standings onto. The library is
    scoped by owner (afc_organizers.OrgLeaderboardDesign.organization):
      * event owned by an organization -> that organization's designs;
      * native AFC event (organization IS NULL) -> the AFC-native library (organization
        IS NULL), which is exactly the library AFC's own standalone leaderboards use.
    So a partner only ever receives the brand art belonging to the event it was granted -
    never another organizer's designs.

    CALLERS: views_partner.event_designs (the can_read_designs endpoint).
    """
    from afc_organizers.models import OrgLeaderboardDesign

    # prefetch_related("logos") folds each design's positioned logos into ONE extra query
    # instead of one per design (serialize_design walks design.logos for every row).
    return (OrgLeaderboardDesign.objects
            .filter(organization=event.organization)      # None -> the AFC-native library
            .prefetch_related("logos")
            .order_by("-is_default", "id"))


def serialize_design(design, partner):
    """One leaderboard design's public template: identity + brand colours + its art.

    Emitted so a broadcaster can reproduce AFC/organizer branding in its own graphics
    package: the two background canvases (Instagram portrait 1080x1350, YouTube landscape
    1920x1080), the positioned logos, and the text/accent colours the renderer draws with.

    Art urls are gated on include_media (they are the licensed brand files) and absolute;
    the colours/flags are cheap descriptive metadata and always emitted so a partner can
    still colour-match when it has not been granted the art itself. No design PK: the
    design's `name` is the handle, matching the no-raw-PKs rule the rest of this file keeps.
    Logo positions are percent-of-canvas, centre-anchored, so they map to BOTH sizes.
    """
    out = {
        "name": design.name,
        "design_type": design.design_type,
        "text_color": design.text_color,
        "accent_color": design.accent_color,
        "transparent_background": design.transparent_background,
        "max_rows": design.max_rows,
        "is_default": design.is_default,
    }
    if partner.include_media:
        out["background_instagram_url"] = _media_url(design.background_instagram)
        out["background_youtube_url"] = _media_url(design.background_youtube)
        # Each positioned logo: where it sits (percent of canvas, centre-anchored) + size.
        out["logos"] = [
            {
                "image_url": _media_url(logo.image),
                "x_pct": logo.x_pct,
                "y_pct": logo.y_pct,
                "size": logo.size,
            }
            for logo in design.logos.all()
        ]
    return out


def _apply_points(entry, partner, values):
    """The points of one row, broken down so they ADD UP, each part behind the toggle that guards
    the stat it reveals (inbox #220 + #223, 2026-10-10).

        points = placement_points + kill_points + other_points
                 + bonus_points - penalty_points + carry_over_points

    Ungated: points (the score the row is ranked by), the two ADMIN ADJUSTMENTS and `adjusted`,
    and the Point-Rush carry-over. An AFC admin or the organizer can add points to a team's
    result on a map (bonus_points) or take them away (penalty_points), for example as a
    punishment for a rules breach; the table then no longer matches placements and kills alone,
    so every row says so plainly instead of leaving a partner to find a discrepancy. AFC keeps no
    written reason for an adjustment, so none is sent.

    Gated: placement_points behind include_placements, kill_points behind include_kills, and
    other_points only when BOTH are on (on its own it would give the two away). other_points is
    what the stored map total holds beyond placement and kill points: assist and damage points,
    the total a results import brought from another platform, and rounding.
    """
    points = values["points"] or 0
    bonus = values["bonus_points"] or 0
    penalty = values["penalty_points"] or 0
    carry = values.get("carry_over_points", 0) or 0
    entry["points"] = points
    entry["bonus_points"] = bonus
    entry["penalty_points"] = penalty
    entry["adjusted"] = bool(bonus or penalty)
    entry["carry_over_points"] = carry
    if partner.include_placements:
        entry["placement_points"] = values["placement_points"] or 0
    if partner.include_kills:
        entry["kill_points"] = values["kill_points"] or 0
    if partner.include_placements and partner.include_kills:
        entry["other_points"] = (points - carry - bonus + penalty
                                 - entry["placement_points"] - entry["kill_points"])


def _apply_standings_fields(entry, partner, values):
    """Copy a standings row's numbers into the public entry. `values` is a dict of NAMED numbers
    built by the caller, never a raw queryset row, so no team or competitor key can ride along.

    Points first (_apply_points), then matches_played (ungated), then the stats each behind its
    toggle: placement and booyahs reveal finishing places (include_placements), kills
    (include_kills), damage and assists their own toggles. damage and assists are absent from
    solo rows because a solo event does not record them.
    """
    _apply_points(entry, partner, values)
    entry["matches_played"] = values["matches_played"] or 0
    if partner.include_placements:
        entry["placement"] = values["placement"]
        entry["booyahs"] = values["booyahs"] or 0
    if partner.include_kills:
        entry["kills"] = values["kills"] or 0
    if partner.include_damage and "damage" in values:
        entry["damage"] = values["damage"] or 0
    if partner.include_assists and "assists" in values:
        entry["assists"] = values["assists"] or 0


# ── the whole event as one tree ────────────────────────────────────────────────
#
# GET /events/<slug>/results/ (owner, inbox #222, 2026-10-10: "can our data that is been sent
# be very properly structured and arranged in a way that it is very easy to understand,
# especially when several structures are mixed into one event").
#
# The flat endpoints answer one question each and leave a partner to reassemble the event:
# /matches/ did not even say which stage or group a map belonged to, and Clash Squad brackets
# were not sent at all. This builds the event the way the tournament page shows it, in running
# order, and says at every level WHAT KIND of thing it is, so a reader never has to infer it:
#
#   event            the event card (= /events/<slug>/): the point system, the final stage and
#                    whether its results are in
#   final_standings  the official final placement (= /standings/)
#   stages[]         in running order; each says its `game` (battle_royale / clash_squad) and
#                    its `structure`:
#                      lobbies      Battle Royale groups, each scored on placement + kills
#                      round_robin  Battle Royale round robin: teams in base groups meet across
#                                   game-day lobbies, ranked on the whole stage
#                      brackets     Clash Squad: each group is a head-to-head bracket
#     standings      the stage's own table (what decides who goes through)
#     groups[]       each says its `type`:
#                      lobby    standings + maps[], each map with its point system and results
#                      bracket  standings (W/D/L, rounds) + matches[] (team_a v team_b, score)
#
# Same firewall rules as everything above: built field by field, names never ids, stats behind
# their toggles. The document is ONE event, so it is not paged; its size is bounded by the
# event's own structure (the largest production event is a few hundred map rows).

# The bracket engine names (StageGroups.bracket_format) in the words a partner reads.
_BRACKET_FORMAT_NAMES = {
    "single_elim": "knockout",
    "double_elim": "double_elimination",
    "league": "league",
    "round_robin_h2h": "round_robin",
}

# HeadToHeadMatch.result_type in the words a partner reads ("normal" is an ordinary played set).
_RESULT_TYPE_NAMES = {"normal": "played", "forfeit": "forfeit", "walkover": "walkover",
                      "dq": "disqualification"}


def _stages_in_running_order(event):
    """The order the tournament page shows stages in: the organizer's manual order, then date."""
    return list(event.stages.order_by("stage_order", "start_date", "stage_id"))


def _groups_in_running_order(stage):
    """The order the tournament page shows a stage's groups in. Synthetic groups are left out:
    head_to_head.write_placement_stats hangs a "Bracket Results" bookkeeping group off a Clash
    Squad stage, and nobody plays in it."""
    return list(stage.groups.filter(is_synthetic=False)
                .order_by("group_order", "playing_date", "playing_time", "group_id"))


def _stage_structure(stage):
    from afc_tournament_and_scrims.stage_formats import is_clash_squad

    if is_clash_squad(stage.stage_format):
        return "brackets"
    if str(stage.stage_format or "").strip().lower() == "br - round robin":
        return "round_robin"
    return "lobbies"


def serialize_results(event, partner):
    """The whole event as one structured document (see the section comment above)."""
    from afc_tournament_and_scrims.views import _final_stage_for_event

    final_stage = _final_stage_for_event(event)
    stages = _stages_in_running_order(event)
    return {
        # The same event card /events/<slug>/ answers (one serializer per shape), so it carries
        # the point system and final_stage / final_stage_has_results.
        "event": serialize_event(event, partner),
        "final_standings": serialize_standings(event, partner),
        "stages": [_results_stage(stage, i, final_stage, partner)
                   for i, stage in enumerate(stages, start=1)],
    }


def _results_stage(stage, order, final_stage, partner):
    from afc_tournament_and_scrims import head_to_head

    solo = stage.event.participant_type == "solo"
    structure = _stage_structure(stage)
    base = serialize_stage(stage, partner)  # the scoring modes, built once
    out = {
        "order": order,
        "stage_name": stage.stage_name,
        "game": "clash_squad" if structure == "brackets" else "battle_royale",
        "structure": structure,
        "format": stage.stage_format,
        "status": stage.stage_status,
        "start_date": stage.start_date,
        "end_date": stage.end_date,
        "is_final_stage": bool(final_stage and final_stage.stage_id == stage.stage_id),
        "teams_qualifying": stage.teams_qualifying_from_stage,
        "champion_point": base["champion_point"],
        "point_rush": base["point_rush"],
        "standings": _stage_table(stage, partner, solo),
    }

    if structure == "round_robin":
        # The base groups the round robin is built on (A, B, C ...), each with its teams. The
        # lobbies below are the game days those groups were merged into.
        out["round_robin_groups"] = [
            {"label": rr.label,
             "teams": sorted(t.display_name for t in rr.teams.select_related("team", "ghost_team"))}
            for rr in stage.round_robin_groups.all()
        ]

    groups = []
    if structure == "brackets" and head_to_head.bracket_matches(stage, None).exists():
        # LEGACY Clash Squad shape: one bracket owned by the whole stage (group NULL).
        groups.append(_results_bracket(stage, None, partner))
    carry = None
    for group in _groups_in_running_order(stage):
        if group.bracket_format:
            groups.append(_results_bracket(stage, group, partner))
        else:
            if carry is None and solo:
                from afc_tournament_and_scrims.views import _carry_over_for_stage
                carry = _carry_over_for_stage(stage, "solo")
            groups.append(_results_lobby(group, partner, solo, carry))
    out["groups"] = groups
    return out


def _stage_table(stage, partner, solo):
    """The stage's own table, the one that decides who goes through: official_stage_standings
    for teams (all its lobbies summed, carry-over and Champion-Point applied), the solo fold
    for players."""
    if solo:
        from afc_tournament_and_scrims.views import _carry_over_for_stage
        return _solo_table(SoloPlayerMatchStats.objects.filter(match__group__stage=stage), partner,
                           _carry_over_for_stage(stage, "solo"))
    from afc_tournament_and_scrims.final_standings import official_stage_standings

    rows = official_stage_standings(stage)
    extras = _team_extras(TournamentTeamMatchStats.objects.filter(match__group__stage=stage))
    logos = _team_logos(r["tournament_team_id"] for r in rows) if partner.include_media else {}
    return [_team_row(i, r, extras, partner, logos) for i, r in enumerate(rows, start=1)]


def _results_lobby(group, partner, solo, solo_carry):
    """One Battle Royale group: its table, then each map with its point system and results."""
    from afc_tournament_and_scrims.models import Match, StageGroupCompetitor

    matches = list(Match.objects.filter(group=group).order_by("match_number"))
    systems = []
    for m in matches:
        system = _point_system(m.scoring_settings)
        if system not in systems:
            systems.append(system)
    out = {
        "group_name": group.group_name,
        "type": "lobby",
        "playing_date": group.playing_date,
        # The game day this lobby belongs to in a round-robin stage, else None.
        "game_day": group.game_day,
        "point_system": systems[0] if len(systems) == 1 else None,
        "point_system_varies": len(systems) > 1,
    }
    if partner.include_maps:
        out["maps"] = list(group.match_maps or [])

    if solo:
        out["standings"] = _solo_table(SoloPlayerMatchStats.objects.filter(match__group=group),
                                       partner, solo_carry)
        present = {r["username"] for r in out["standings"]}
        for sc in (StageGroupCompetitor.objects.filter(stage_group=group, player__isnull=False)
                   .select_related("player__user")):
            user = sc.player.user if sc.player else None
            if user and user.username not in present:
                out["standings"].append(_zero_row(len(out["standings"]) + 1, partner, {
                    "username": user.username, "in_game_id": user.uid, **_player_country(user),
                    **({"esports_image_url": _player_image(user)} if partner.include_media else {})}))
    else:
        from afc_tournament_and_scrims.final_standings import official_group_standings

        rows = official_group_standings(group)
        extras = _team_extras(TournamentTeamMatchStats.objects.filter(match__group=group))
        logos = _team_logos(r["tournament_team_id"] for r in rows) if partner.include_media else {}
        out["standings"] = [_team_row(i, r, extras, partner, logos)
                            for i, r in enumerate(rows, start=1)]
        # Champion-Point: the team the rule crowned in this lobby, by name; None when the stage
        # does not use it or nobody has triggered it yet.
        if group.stage.champion_point_enabled:
            out["champion"] = next((r["team_name"] for r in rows if r.get("is_champion")), None)
        # Teams drawn into the group with no result yet: listed at 0, after the ranked rows, as
        # the tournament page lists them, so an upcoming group still shows who is in it.
        present = {r["tournament_team_id"] for r in rows}
        for sc in (StageGroupCompetitor.objects.filter(stage_group=group, tournament_team__isnull=False)
                   .select_related("tournament_team__team", "tournament_team__ghost_team")):
            tt = sc.tournament_team
            if tt.pk not in present:
                present.add(tt.pk)
                out["standings"].append(_zero_row(len(out["standings"]) + 1, partner, {
                    "team": tt.display_name,
                    **_country_fields(tt.competitor.country if tt.competitor else None),
                    **({"logo_url": None if tt.is_ghost else _media_url(tt.team.team_logo)}
                       if partner.include_media else {})}))

    out["matches"] = [_results_map(m, partner, solo) for m in matches]
    return out


def _zero_row(rank, partner, identity):
    """A standings row for a competitor drawn into a group that has not played yet."""
    entry = {"rank": rank, **identity}
    _apply_standings_fields(entry, partner, {
        "points": 0, "carry_over_points": 0, "placement_points": 0, "kill_points": 0,
        "bonus_points": 0, "penalty_points": 0, "booyahs": 0, "matches_played": 0,
        "placement": None, "kills": 0, **({} if "username" in identity else {"damage": 0, "assists": 0}),
    })
    return entry


def _results_map(match, partner, solo):
    """One Battle Royale map: number, status, its point system, and every competitor's result,
    best finish first (competitors who did not play last)."""
    out = serialize_match(match, partner)
    rows = []
    if solo:
        stats = (SoloPlayerMatchStats.objects.filter(match=match)
                 .select_related("competitor__user"))
        for s in stats:
            user = s.competitor.user if s.competitor else None
            entry = {"username": user.username if user else None,
                     "in_game_id": user.uid if user else None,
                     **(_player_country(user) if user else _country_fields(None)),
                     "played": bool(s.played)}
            _apply_map_fields(entry, partner, s, solo=True)
            rows.append(entry)
    else:
        stats = (TournamentTeamMatchStats.objects.filter(match=match)
                 .select_related("tournament_team__team", "tournament_team__ghost_team"))
        for s in stats:
            tt = s.tournament_team
            entry = {"team": tt.display_name,
                     **_country_fields(tt.competitor.country if tt.competitor else None),
                     "played": bool(s.played)}
            _apply_map_fields(entry, partner, s, solo=False)
            rows.append(entry)
    rows.sort(key=lambda e: (not e["played"], e.get("_sort_place") or 999, -e["points"]))
    for e in rows:
        e.pop("_sort_place", None)
    out["results"] = rows
    return out


def _apply_map_fields(entry, partner, s, solo):
    """One competitor's result on one map: its points broken down exactly as a table row's are
    (_apply_points, so an admin's bonus or penalty on THIS map shows here, with `adjusted`), then
    the stats behind their toggles. A team that sat the map out has placement None."""
    placement = s.placement or None
    entry["_sort_place"] = placement  # sort key only, removed before the row is returned
    if solo:
        # Solo stored total_points leaves bonus and penalty out; the tables count them, so this does.
        points = ((s.placement_points or 0) + (s.kill_points or 0)
                  + (s.bonus_points or 0) - (s.penalty_points or 0))
    else:
        points = s.total_points or 0
    _apply_points(entry, partner, {
        "points": points, "bonus_points": s.bonus_points, "penalty_points": s.penalty_points,
        "placement_points": s.placement_points, "kill_points": s.kill_points})
    # A single map has no Point-Rush head start; that belongs to a table.
    entry.pop("carry_over_points", None)
    if partner.include_placements:
        entry["placement"] = placement
    if partner.include_kills:
        entry["kills"] = s.kills or 0
    if not solo and partner.include_damage:
        entry["damage"] = s.damage or 0
    if not solo and partner.include_assists:
        entry["assists"] = s.assists or 0


def _results_bracket(stage, group, partner):
    """One Clash Squad bracket: its table and its matches. group None = the legacy stage-wide
    bracket. Scores are ROUNDS won in the set (4-2), never kills."""
    from afc_tournament_and_scrims import head_to_head
    from afc_tournament_and_scrims.stage_formats import legacy_bracket_mode

    group_id = group.group_id if group else None
    engine = group.bracket_format if group else legacy_bracket_mode(stage.stage_format)
    fmt = _BRACKET_FORMAT_NAMES.get(engine, engine)
    league = fmt in ("league", "round_robin")

    # Upper bracket (and a league's single list) first, then the lower bracket, then the bronze
    # match; inside each, round by round, top to bottom.
    side_order = {"winners": 0, "league": 0, "losers": 1, "third": 2}
    matches = sorted(
        head_to_head.bracket_matches(stage, group_id)
        .select_related("team_a__team", "team_a__ghost_team", "team_b__team", "team_b__ghost_team",
                        "winner__team", "winner__ghost_team"),
        key=lambda m: (side_order.get(m.bracket, 3), m.round_number, m.position))
    teams = {}  # tournament_team_id -> its public identity (name, country, logo)
    for m in matches:
        for tt in (m.team_a, m.team_b):
            if tt is not None and tt.pk not in teams:
                teams[tt.pk] = {"team": tt.display_name,
                                **_country_fields(tt.competitor.country if tt.competitor else None)}
                if partner.include_media:
                    teams[tt.pk]["logo_url"] = None if tt.is_ghost else _media_url(tt.team.team_logo)

    def side(tt):
        if tt is None:
            return None  # an empty slot: waiting on an earlier match, or a bye
        return dict(teams[tt.pk])

    table = []
    for r in head_to_head.standings(stage, group_id):
        entry = {"rank": r["placement"],
                 **teams.get(r["tournament_team_id"], {"team": r["team_name"]}),
                 "wins": r["wins"], "draws": r["draws"], "losses": r["losses"],
                 "rounds_won": r["rounds_won"], "rounds_lost": r["rounds_lost"]}
        if league:
            # League points (3 a win, 1 a draw), what a league table ranks on. A knockout ranks on
            # how far a team went, so it has none.
            entry["points"] = r["points"]
        table.append(entry)

    return {
        "group_name": group.group_name if group else None,
        "type": "bracket",
        "bracket_format": fmt,
        "playing_date": group.playing_date if group else None,
        "standings": table,
        "matches": [{
            "bracket": m.bracket,
            "round": m.round_number,
            "position": m.position,
            "team_a": side(m.team_a),
            "team_b": side(m.team_b),
            "score_a": m.score_a,
            "score_b": m.score_b,
            "winner": m.winner.display_name if m.winner else None,
            "status": m.status,
            "result": _RESULT_TYPE_NAMES.get(m.result_type, m.result_type),
            "scheduled_date": m.scheduled_date,
        } for m in matches],
    }
