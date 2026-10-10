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

    `order` is computed from the stage's position among its event's stages (ordered
    by stage_id, the same ordering the admin standings view uses) rather than exposing
    the raw stage_id - partners get a stable sequence number, never a DB PK.
    """
    # Position of this stage among its siblings, ordered by stage_id (creation order).
    # Counting stages created before-or-at this one yields a 1-based ordinal.
    order = (stage.event.stages.filter(stage_id__lte=stage.stage_id).count())
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
        from afc_auth.models import canonical_profile

        profile = canonical_profile(user)
        out["esports_image_url"] = _media_url(profile.esports_pic) if profile else None

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
# ONE RANKING, THE SITE'S (inbox #220, 2026-10-10). Squad and duo standings are built by
# afc_tournament_and_scrims.round_robin._aggregate_team_standings over every map of the
# event: the shared core behind the event page's Combined tab (whole event), the broadcast
# overlay feed and advancement seeding. A partner's table is therefore the table a visitor
# sees on the Combined tab, row for row.
#
# This module used to fold the rows itself, and the copy had drifted from the site in three
# ways. Measured on production 2026-10-10 over the 72 events a partner reads: 7 ranked
# differently from the site, and 1 merged every imported team into a single nameless row.
#   1. The score. The site ranks by the STORED per-map total_points (placement + kill +
#      ASSIST + DAMAGE + bonus - penalty, written by scoring.compute_team_points). The copy
#      re-derived placement + kill + bonus - penalty, dropping assist and damage points.
#   2. Ties. The site breaks a tie on points by booyahs, kills, then the placement in the
#      last map played, or by the order the organizer arranged (event.tie_breakers). The copy
#      stopped at kills.
#   3. Imported teams. The copy grouped by tournament_team__team__team_name, which is NULL for
#      every ghost team, so all of them collapsed into one row named null.
#
# Solo events have no shared event-wide core (the site's Combined tab is team only), so the
# solo fold stays here, scored the way the admin standings view scores a solo lobby:
# placement + kill + bonus - penalty (a solo row's stored total_points leaves bonus and
# penalty out, scoring.compute_solo_points).


def serialize_standings(event, partner):
    """Event-wide standings: ranked rows carrying a public handle, the country, the score
    (`points`) and the toggled stats. Never a competitor or team PK.

    Solo events rank players (username + in_game_id); squad and duo events rank teams.
    """
    if event.participant_type == "solo":
        return _solo_standings(event, partner)
    return _team_standings(event, partner)


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


def _team_standings(event, partner):
    from afc_tournament_and_scrims import round_robin

    stats = TournamentTeamMatchStats.objects.filter(match__group__stage__event=event)
    ranked = round_robin._aggregate_team_standings(stats, event=event)

    # The three columns a partner already received that the shared core does not carry: damage,
    # assists and the best finish. One grouped query, joined on tournament_team_id, which is used
    # here as a dict key and never emitted. Best finish is over PLAYED maps (a map a team sat
    # out is stored with placement 0).
    extra = {
        r["tournament_team_id"]: r
        for r in stats.values("tournament_team_id").annotate(
            damage=Coalesce(Sum("damage"), 0),
            assists=Coalesce(Sum("assists"), 0),
            best_placement=Min("placement", filter=Q(placement__gt=0)),
        )
    }

    out = []
    for i, r in enumerate(ranked, start=1):
        more = extra.get(r["tournament_team_id"], {})
        entry = {"rank": i, "team": r["team_name"]}
        entry.update(_country_fields(r.get("team_country")))
        _apply_standings_fields(entry, partner, {
            "points": r["effective_total"],
            "placement_points": r["placement_sum"],
            "kill_points": r["kill_sum"],
            "bonus_points": r["bonus_sum"],
            "penalty_points": r["penalty_sum"],
            "booyahs": r["total_booyah"],
            "matches_played": r["games_played"],
            "placement": more.get("best_placement"),
            "kills": r["total_kills"],
            "damage": more.get("damage", 0),
            "assists": more.get("assists", 0),
        })
        out.append(entry)
    return out


def _solo_standings(event, partner):
    rows = (
        SoloPlayerMatchStats.objects
        .filter(match__group__stage__event=event)
        # Both country columns ride along for the player-flag rule (ip_country or country). They
        # belong to the same user, so they do not split the GROUP BY.
        .values("competitor__user__username", "competitor__user__uid",
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
        .order_by("-points", "-booyahs", "-kills")
    )
    out = []
    for i, r in enumerate(rows, start=1):
        entry = {
            "rank": i,
            "username": r["competitor__user__username"],
            "in_game_id": r["competitor__user__uid"],
        }
        entry.update(_country_fields(r["competitor__user__ip_country"] or r["competitor__user__country"]))
        _apply_standings_fields(entry, partner, {
            "points": r["points"],
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


def _apply_standings_fields(entry, partner, values):
    """Copy a standings row's numbers into the public entry, each behind the toggle that guards
    the stat it reveals. `values` is a dict of NAMED numbers built by the caller, never a raw
    queryset row, so no team or competitor key can ride along.

    Ungated (inbox #220): points, the score the row is ranked by, with the adjustments it
    includes (bonus_points, penalty_points) and matches_played. Gated: placement,
    placement_points and booyahs reveal finishing places (include_placements); kills and
    kill_points reveal kills (include_kills); damage and assists their own toggles. damage and
    assists are absent from solo rows because a solo event does not record them.
    """
    entry["points"] = values["points"] or 0
    entry["bonus_points"] = values["bonus_points"] or 0
    entry["penalty_points"] = values["penalty_points"] or 0
    entry["matches_played"] = values["matches_played"] or 0
    if partner.include_placements:
        entry["placement"] = values["placement"]
        entry["placement_points"] = values["placement_points"] or 0
        entry["booyahs"] = values["booyahs"] or 0
    if partner.include_kills:
        entry["kills"] = values["kills"] or 0
        entry["kill_points"] = values["kill_points"] or 0
    if partner.include_damage and "damage" in values:
        entry["damage"] = values["damage"] or 0
    if partner.include_assists and "assists" in values:
        entry["assists"] = values["assists"] or 0
