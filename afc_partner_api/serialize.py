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
#   • internal config/flags - scoring_settings, rankings_verified, is_draft, creator,
#                             partner_published
# `is_native_afc` is derived as `organization_id is None` (a boolean), so partners
# learn an event is a native AFC event WITHOUT ever receiving the raw org PK.
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

    Documented for partners in WEBSITE/PARTNER_API.md ("Team participation status"). The values
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
    out = {"team": tt.team.team_name, "team_tag": tt.team.team_tag}

    # Team BRAND art: the logo a broadcaster puts next to the team's name. Absolute url,
    # None when the team never uploaded one.
    if partner.include_media:
        out["logo_url"] = _media_url(tt.team.team_logo)
    # Team COPY: the short self-description shown on the team's site profile.
    if partner.include_text:
        out["description"] = tt.team.team_description or None

    # Aggregate this team's finalized per-match stat rows once (avoids N queries below).
    # played_matches rides along in the SAME aggregate (a filtered Count, so it costs no extra
    # query and leaves every other total folded over ALL rows exactly as before) and feeds
    # team_status: it counts only the matches the team actually turned up for.
    agg = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team=tt)
        .aggregate(
            best_placement=Min("placement"),
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
# Ranking metric - why `effective_total`, not the stored `total_points` column:
# the official admin standings view (afc_tournament_and_scrims.views.
# get_all_leaderboard_details_for_event) does NOT trust the persisted total_points
# column - it RECOMPUTES the rank metric on read as
#       effective_total = placement_points + kill_points + bonus_points - penalty_points
# precisely because total_points can be STALE (e.g. a bonus/penalty edited after the
# row was first saved). To keep partner rankings aligned with official AFC standings we
# compute the SAME effective_total here and order by it, with the admin view's leading
# tiebreakers that are well-defined over an event-wide fold:
#       -effective_total, -total_booyah (1st-place finishes), -total_kills.
#
# Honest scope note (so this comment can't drift false like the old one): the admin view
# is computed PER LOBBY/GROUP and carries two extra steps we deliberately do NOT
# replicate in this event-wide partner aggregate - (a) its final `last_match_placement`
# tiebreaker (a per-group "placement in the latest match" subquery) and (b) the
# scoring-mode carry-over overlay. Those are lobby-local; the partner standings are a
# single event-wide ranking. We match the admin's PRIMARY ordering exactly; the residual
# last-match tiebreaker only ever matters when effective_total, booyah, AND kills all tie.


def serialize_standings(event, partner):
    """Event-wide final standings: a ranked list of competitors with public handle +
    toggled stats. Reads from the ALREADY-FINALIZED stat rows and ranks by the same
    recomputed `effective_total` metric the admin standings view uses (see the module
    comment above for why total_points is NOT trusted), then assigns a 1-based `rank`.

    Solo events fold SoloPlayerMatchStats by competitor; squad/duo events fold
    TournamentTeamMatchStats by team. Either way we emit ONLY a public handle, the
    rank, and the toggled-on stat fields - never the underlying competitor/team PK.
    """
    if event.participant_type == "solo":
        return _solo_standings(event, partner)
    return _team_standings(event, partner)


# Booyah = a 1st-place finish. Counting these mirrors the admin view's `total_booyah`
# tiebreaker (Sum of "1 when placement==1 else 0") so partner and official ties break
# the same way.
_BOOYAH = Sum(Case(When(placement=1, then=Value(1)), default=Value(0),
                   output_field=IntegerField()))

# Recomputed-on-read rank metric, identical to the admin view's `effective_total`
# (placement + kill + bonus - penalty). We never order by the stored total_points,
# which can be stale.
_EFFECTIVE_TOTAL = (
    Coalesce(Sum("placement_points"), 0)
    + Coalesce(Sum("kill_points"), 0)
    + Coalesce(Sum("bonus_points"), 0)
    - Coalesce(Sum("penalty_points"), 0)
)


def _team_standings(event, partner):
    # Fold every team's finalized match rows in this event into one summary per team,
    # then rank by recomputed effective_total (admin parity), booyahs, kills - winners
    # first. total_points is still summed only to expose it; it is NOT the sort key.
    rows = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team__event=event)
        .values("tournament_team__team__team_name")
        .annotate(
            effective_total=_EFFECTIVE_TOTAL,
            total_booyah=_BOOYAH,
            total_points=Sum("total_points"),
            kills=Sum("kills"),
            damage=Sum("damage"),
            assists=Sum("assists"),
            best_placement=Min("placement"),
            matches_played=Count("team_stats_id"),
        )
        .order_by("-effective_total", "-total_booyah", "-kills")
    )
    out = []
    for i, r in enumerate(rows, start=1):
        # rank is a derived ordinal; team_name is the public handle. No PKs.
        entry = {"rank": i, "team": r["tournament_team__team__team_name"]}
        _apply_standings_toggles(entry, r, partner)
        out.append(entry)
    return out


def _solo_standings(event, partner):
    rows = (
        SoloPlayerMatchStats.objects
        .filter(match__group__stage__event=event)
        .values("competitor__user__username", "competitor__user__uid")
        .annotate(
            effective_total=_EFFECTIVE_TOTAL,
            total_booyah=_BOOYAH,
            total_points=Sum("total_points"),
            kills=Sum("kills"),
            best_placement=Min("placement"),
            matches_played=Count("id"),
        )
        .order_by("-effective_total", "-total_booyah", "-kills")
    )
    out = []
    for i, r in enumerate(rows, start=1):
        entry = {
            "rank": i,
            "username": r["competitor__user__username"],
            "in_game_id": r["competitor__user__uid"],
        }
        _apply_standings_toggles(entry, r, partner)
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


def _apply_standings_toggles(entry, row, partner):
    """Copy ONLY the toggled-on aggregated stats from an annotated standings row into the
    public entry. `row` is a dict from a .values().annotate() queryset; we never spread
    it wholesale (that would leak the competitor/team key), only pull named stats."""
    if partner.include_placements:
        entry["placement"] = row.get("best_placement")
    if partner.include_kills:
        entry["kills"] = row.get("kills") or 0
    # damage/assists only exist on the team aggregate; .get() is None for solo rows.
    if partner.include_damage and "damage" in row:
        entry["damage"] = row.get("damage") or 0
    if partner.include_assists and "assists" in row:
        entry["assists"] = row.get("assists") or 0
