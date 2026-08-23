"""
afc_results_import.services - resolve competitors and write an import.

Two phases, and the split IS the safety property:

  PREVIEW  parses, resolves and validates, and writes NOTHING. A bad file produces a report.
  COMMIT   writes everything inside one transaction, only after an admin has seen that report.

IDENTITY (spec section 7). Resolution order, first hit wins:
    1. an alias row for this event      an admin's recorded decision always wins
    2. a team already registered here   the obvious candidate
    3. any site team by normalised name
    4. an EXISTING ghost by normalised name
    5. create a new ghost

Step 4 matters more than it looks. Without it, importing FFWS Spring and then FFWS Fall would create
two unrelated ghosts for one club, and a later claim would inherit half its history.

RE-IMPORT IS REPLACE, NOT APPEND, keyed on (event, stage, group): the group's imported rows and its
synthetic match are deleted, then written fresh. Only rows whose Match.upload_method is
"xlsx_import" are eligible, so an import can never destroy a result somebody entered by hand or
uploaded from a match log.

Connects to: afc_tournament_and_scrims (Event/Stages/StageGroups/Match/TournamentTeam/
TournamentTeamMatchStats/StageCompetitor/StageGroupCompetitor), afc_team.Team, afc_rankings.GhostTeam.
"""
from django.db import transaction
from django.utils import timezone

from afc_rankings.models import GhostPlayer, GhostTeam
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Match, StageCompetitor, StageGroupCompetitor, Stages, StageGroups,
    TournamentTeam, TournamentPlayerMatchStats, TournamentTeamMatchStats,
)

from .models import ExternalResultTeamAlias
from .parsing import parse_workbook

# The marker that makes a row OURS. Deletion on re-import is scoped to this, which is what stops an
# import from ever removing a hand-entered or log-uploaded result.
UPLOAD_METHOD = "xlsx_import"


def norm_team_name(name):
    """Fold a team name for matching, reusing the tournament app's normaliser.

    That function already handles the cases this data really contains: clan tags, decorative unicode
    (the published FFWS standings include a "TGR[]LEGENDS"), and spacing. Imported lazily because
    afc_tournament_and_scrims.views is a very large module and this app should not drag it in at
    import time.
    """
    try:
        from afc_tournament_and_scrims.views import _norm_tname
        return _norm_tname(name)
    except Exception:
        # A conservative fallback with the same SHAPE, so behaviour degrades rather than breaking if
        # that private helper is ever renamed. Casefold + strip non-alphanumerics.
        import re
        return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _norm_player_name(name):
    """Compare in-game names the way team names are compared: case and spacing do not identify a
    person, so "Ali Ff", "ALI FF" and "aliff" are the same player in one event."""
    return "".join(str(name or "").split()).casefold()


def _match_player_to_user(tournament_team, name):
    """The REAL AFC user this in-game name means, or None.

    Only ever looks inside the team's OWN registered roster for this event. A global username
    search would be worse than useless here: an external tournament's "Sniper" is not AFC's
    "Sniper", and silently attributing a stranger's kills to a real account would corrupt that
    person's profile and their ranking. Scoped matching means a real team importing its own
    match-by-match results gets its real players credited, and nothing else does.
    """
    if tournament_team.team_id is None:
        return None
    want = _norm_player_name(name)
    if not want:
        return None
    # TournamentTeamMember is the FROZEN per-event roster, which is the right population: who was
    # actually fielded for THIS event, not who is on the team today.
    from afc_tournament_and_scrims.models import TournamentTeamMember
    for member in (TournamentTeamMember.objects
                   .filter(tournament_team=tournament_team)
                   .select_related("user")):
        user = getattr(member, "user", None)
        if user is None:
            continue
        for candidate in (user.username, getattr(user, "uid", None)):
            if candidate and _norm_player_name(candidate) == want:
                return user
    return None


def _ghost_player_for(tournament_team, name, *, actor=None):
    """The GhostPlayer for an in-game name on a GHOST team, created on first sight.

    `actor` is accepted and unused: GhostPlayer records no creator (unlike GhostTeam), and the
    import that produced the row is already recorded on Event.results_imported_by.

    Returns None for a REAL team, deliberately. A ghost player hangs off a ghost team, and inventing
    one under a real AFC team would put a name on that team's public page which its owner never
    added. The caller reports those rows instead of guessing.
    """
    if tournament_team.ghost_team_id is None:
        return None
    want = _norm_player_name(name)
    for existing in GhostPlayer.objects.filter(ghost_team_id=tournament_team.ghost_team_id):
        if _norm_player_name(existing.ign) == want:
            return existing
    # slot is the roster's display order and GhostPlayer.Meta orders by it, so append rather than
    # leaving every imported player on the default slot 1, which would order a roster arbitrarily.
    next_slot = GhostPlayer.objects.filter(
        ghost_team_id=tournament_team.ghost_team_id).count() + 1
    return GhostPlayer.objects.create(
        ghost_team_id=tournament_team.ghost_team_id, ign=str(name).strip(), slot=next_slot)


def resolve_competitor(event, source_name, *, actor=None, create_missing=True):
    """Find or create the TournamentTeam a spreadsheet name refers to.

    Returns (tournament_team, resolution, created_ghost). `resolution` is one of the
    ExternalResultTeamAlias.RESOLUTION values and is what the preview shows the admin.
    """
    normalized = norm_team_name(source_name)

    alias = ExternalResultTeamAlias.objects.filter(
        event=event, normalized_name=normalized).select_related("tournament_team").first()
    if alias and alias.tournament_team_id:
        return alias.tournament_team, "manually_paired", False

    # 2) already registered to THIS event, real teams first
    for tt in TournamentTeam.objects.filter(event=event).select_related("team", "ghost_team"):
        if norm_team_name(tt.display_name) == normalized:
            return tt, ("matched_ghost" if tt.is_ghost else "auto_matched"), False

    # 3) any site team.
    #    NOTE the create_missing guard around get_or_create. In PREVIEW mode this function must not
    #    write ANYTHING, and registering a matched team to the event is a write. Preview reports the
    #    match and returns an UNSAVED TournamentTeam purely so the caller can read display_name.
    #    (Caught by test_preview_writes_nothing: the earlier version registered every matched team
    #    during preview, which quietly defeated the whole two-phase safety property.)
    for team in Team.objects.all().only("team_id", "team_name").iterator():
        if norm_team_name(team.team_name) == normalized:
            if not create_missing:
                return TournamentTeam(event=event, team=team), "auto_matched", False
            tt, _ = TournamentTeam.objects.get_or_create(event=event, team=team)
            return tt, "auto_matched", False

    # 4) an existing ghost, so a club imported from a previous tournament keeps ONE identity
    for ghost in GhostTeam.objects.filter(is_active=True).only("ghost_team_id", "team_name"):
        if norm_team_name(ghost.team_name) == normalized:
            if not create_missing:
                return TournamentTeam(event=event, ghost_team=ghost), "matched_ghost", False
            tt, _ = TournamentTeam.objects.get_or_create(event=event, ghost_team=ghost)
            return tt, "matched_ghost", False

    if not create_missing:
        return None, "skipped", False

    # 5) a competitor AFC has never seen
    ghost = GhostTeam.objects.create(
        team_name=str(source_name).strip()[:200], country="", created_by=actor,
    )
    tt = TournamentTeam.objects.create(event=event, ghost_team=ghost)
    return tt, "auto_created_ghost", True


def _near_misses(event, source_name, limit=3):
    """Existing names that LOOK like this one, for the review list.

    A near miss is the duplicate risk: "LAXUS E-SPORTS" created fresh while "Laxus Esports" already
    exists. Compared on a loose prefix of the normalised form, which is enough to surface the case
    without pretending to be a real fuzzy matcher.
    """
    normalized = norm_team_name(source_name)
    if len(normalized) < 4:
        return []
    stem = normalized[:max(4, len(normalized) // 2)]
    hits = []
    for team in Team.objects.all().only("team_name").iterator():
        other = norm_team_name(team.team_name)
        if other != normalized and other.startswith(stem):
            hits.append(team.team_name)
            if len(hits) >= limit:
                break
    return hits


def build_preview(event, data):
    """Parse and resolve WITHOUT WRITING ANYTHING. Returns the dict stored on ResultsImport.preview.

    Resolution here is read-only: it reports what WOULD be created rather than creating it, so an
    admin can see "118 new ghosts" before agreeing to them.
    """
    parsed = parse_workbook(data)
    problems = list(parsed["problems"])
    sheets_out, seen = [], {}

    for sheet in parsed["sheets"]:
        names = []
        for row in sheet["rows"]:
            name = row["team"]
            if name in seen:
                continue
            seen[name] = True
            tt, resolution, _ = resolve_competitor(event, name, create_missing=False)
            entry = {"name": name, "resolution": resolution,
                     "matched": tt.display_name if tt else None}
            if tt is None:
                entry["resolution"] = "auto_created_ghost"
                entry["will_create"] = True
                near = _near_misses(event, name)
                if near:
                    entry["near_misses"] = near
                    problems.append(
                        f"{name!r} will be created as a new competitor, but AFC already has "
                        f"{', '.join(repr(n) for n in near)}. Pair them if they are the same club."
                    )
            names.append(entry)

        sheets_out.append({
            "sheet": sheet["group"], "kind": sheet["kind"],
            "row_count": len(sheet["rows"]), "competitors": names,
        })

    return {
        "sheets": sheets_out,
        "problems": problems,
        "total_rows": sum(s["row_count"] for s in sheets_out),
        "to_create": sum(1 for s in sheets_out for c in s["competitors"] if c.get("will_create")),
        "matched": sum(1 for s in sheets_out for c in s["competitors"] if not c.get("will_create")),
    }


def _target_group(event, sheet_name, stage_hint=None):
    """Find the StageGroups a sheet refers to, by name, within this event.

    Matched loosely (case and punctuation insensitive) against "<stage> - <group>" and against the
    group name alone, because a sheet is realistically called "Phase 1 - Group A", "Group A" or just
    "A". Returns None when nothing matches, and the caller reports that rather than guessing.

    TWO PASSES, EXACT BEFORE LOOSE, and the loose pass must be UNAMBIGUOUS (bug, owner 2026-08-23).
    A multi-phase tournament reuses group letters: FFWS Africa 2026 Fall has Phase 1 Groups A-L AND
    Phase 2 Groups A-D. The previous single loop returned the first group that satisfied EITHER
    test, so the sheet "Phase 2 - Group A" could match the Phase 1 Group A row on the
    `want.endswith(group_name)` fallback ("phase2groupa" ends with "groupa") before ever reaching
    the exact "phase2groupa" == combined match. Which one won depended on nothing but the queryset's
    row order.

    That was silent DATA LOSS, not a mismatch: commit_import REPLACES the rows in whichever group it
    is handed, so all four Phase 2 sheets landed on Phase 1 Groups A-D and destroyed the real Phase 1
    results, while the report said 192 rows across 16 groups with no unmatched sheets.

    So: every exact match is tried across ALL groups first, and the loose fallback resolves only when
    exactly ONE group fits. An ambiguous sheet name returns None and is reported as unmatched, which
    is the honest answer and leaves the admin able to rename the sheet.
    """
    want = norm_team_name(sheet_name)
    if not want:
        return None
    groups = list(StageGroups.objects
                  .filter(stage__event=event)
                  .select_related("stage"))

    def _combined(g):
        return norm_team_name(f"{g.stage.stage_name} {g.group_name}")

    # Pass 1: exact, on "<stage> <group>" first and then on the group name alone. Checking the
    # combined form across every group BEFORE any group-name-only match is what stops "Phase 2 -
    # Group A" from being answered by Phase 1's Group A.
    for g in groups:
        if want == _combined(g):
            return g
    exact_by_group_name = [g for g in groups if want == norm_team_name(g.group_name)]
    if len(exact_by_group_name) == 1:
        return exact_by_group_name[0]
    if len(exact_by_group_name) > 1:
        # "Group A" alone in an event with two Phase-A groups genuinely cannot be resolved.
        return None

    # Pass 2: loose suffix match ("Standings Group A" -> Group A), narrowed by the stage hint when
    # the sheet carried a STAGE column. Resolves ONLY when a single group survives.
    candidates = [g for g in groups
                  if norm_team_name(g.group_name) and want.endswith(norm_team_name(g.group_name))]
    if stage_hint:
        hint = norm_team_name(stage_hint)
        candidates = [g for g in candidates if hint in _combined(g)]
    return candidates[0] if len(candidates) == 1 else None


@transaction.atomic
def commit_import(imp, data, *, actor=None):
    """Write the workbook into the tournament tables. All or nothing.

    Raises rather than returning an error Response: this runs inside transaction.atomic(), and
    returning early from inside an atomic block silently discards the writes made before it (a trap
    this codebase has hit before, recorded in memory reference_atomic_early_return_dataloss).
    Validation belongs in build_preview.
    """
    event = imp.event
    parsed = parse_workbook(data)
    summary = {"groups": [], "created_ghosts": 0, "stats_rows": 0, "replaced_rows": 0,
               "unmatched_sheets": []}

    for sheet in parsed["sheets"]:
        group = _target_group(event, sheet["group"])
        if group is None:
            summary["unmatched_sheets"].append(sheet["group"])
            continue

        # REPLACE, not append. Scoped to rows this importer wrote, so a hand-entered result in the
        # same group survives untouched.
        old = Match.objects.filter(group=group, upload_method=UPLOAD_METHOD)
        summary["replaced_rows"] += TournamentTeamMatchStats.objects.filter(match__in=old).count()
        old.delete()

        per_match_cache = {}
        for row in sheet["rows"]:
            tt, resolution, created = resolve_competitor(event, row["team"], actor=actor)
            if created:
                summary["created_ghosts"] += 1

            ExternalResultTeamAlias.objects.update_or_create(
                event=event, normalized_name=norm_team_name(row["team"]),
                defaults={"source_name": row["team"], "tournament_team": tt,
                          "resolution": resolution, "resolved_by": actor},
            )

            # Appearing in a stage's sheet IS the statement that the team is in that stage and group
            # (spec section 8). FFWS Phase 1 advances "top 3 per group plus the 7 best 4th places",
            # a cross-group rule with its own tie-break chain that no per-stage qualifying number
            # can express, so membership is taken from the file rather than derived.
            StageCompetitor.objects.get_or_create(stage=group.stage, tournament_team=tt)
            StageGroupCompetitor.objects.get_or_create(stage_group=group, tournament_team=tt)

            if sheet["kind"] == "summed":
                match = per_match_cache.get("__aggregate__")
                if match is None:
                    match = Match.objects.create(
                        group=group, match_number=1, match_map="multiple",
                        upload_method=UPLOAD_METHOD, result_inputted=True,
                        played_on=group.playing_date,
                    )
                    per_match_cache["__aggregate__"] = match
                TournamentTeamMatchStats.objects.update_or_create(
                    match=match, tournament_team=tt,
                    defaults={
                        "placement": None, "is_aggregate": True,
                        "matches_counted": row["matches"], "booyah_count": row["booyah"],
                        "final_position": row["position"],
                        "placement_points": row["score"], "kills": row["elims"],
                        "total_points": row["total"],
                    },
                )
            elif sheet["kind"] == "per_match_players":
                # ONE ROW PER PLAYER PER MAP. The team's line for that map is REBUILT from its
                # players' rows rather than read from the file: placement is the team's finish
                # (repeated on each of its player rows, so the first non-null one is it) and the
                # team's kills are the sum of its players'. That keeps the team total and the
                # player breakdown arithmetically consistent by construction, which a file typed by
                # hand does not guarantee.
                key = row["match"]
                match = per_match_cache.get(key)
                if match is None:
                    match = Match.objects.create(
                        group=group, match_number=key,
                        match_map=(row["map"] or "multiple"),
                        upload_method=UPLOAD_METHOD, result_inputted=True,
                        played_on=group.playing_date,
                    )
                    per_match_cache[key] = match

                team_stat, _ = TournamentTeamMatchStats.objects.get_or_create(
                    match=match, tournament_team=tt,
                    defaults={"placement": row["placement"], "kills": 0,
                              "is_aggregate": False, "matches_counted": 1},
                )
                if team_stat.placement is None and row["placement"] is not None:
                    team_stat.placement = row["placement"]

                # The player's identity. A ghost PLAYER hangs off the ghost TEAM when there is one,
                # which is what lets afc_rankings.claims.reattribute_ghost_player move this history
                # onto a real user later. get_or_create keyed on (ghost_team, ign) so re-importing
                # a corrected file reuses the same person instead of creating a second one.
                ghost_player = None
                real_user = _match_player_to_user(tt, row["player"])
                if real_user is None:
                    ghost_player = _ghost_player_for(tt, row["player"], actor=actor)
                    if ghost_player is None:
                        # A real AFC team whose player is not on its roster. Recording an invented
                        # ghost under a real team would put a name on that team's page that its
                        # owner never added, so the row is reported and skipped instead.
                        summary.setdefault("unmatched_players", []).append(
                            f"{row['player']} ({tt.display_name})")
                        continue

                TournamentPlayerMatchStats.objects.update_or_create(
                    team_stats=team_stat,
                    player=real_user, ghost_player=ghost_player,
                    defaults={"kills": row["kills"] or 0},
                )
                team_stat.kills = sum(
                    ps.kills for ps in team_stat.player_stats.all())
                team_stat.save(update_fields=["placement", "kills"])
            else:
                key = row["match"]
                match = per_match_cache.get(key)
                if match is None:
                    match = Match.objects.create(
                        group=group, match_number=key,
                        match_map=(row["map"] or "multiple"),
                        upload_method=UPLOAD_METHOD, result_inputted=True,
                        played_on=group.playing_date,
                    )
                    per_match_cache[key] = match
                TournamentTeamMatchStats.objects.update_or_create(
                    match=match, tournament_team=tt,
                    defaults={"placement": row["placement"], "kills": row["kills"],
                              "is_aggregate": False, "matches_counted": 1},
                )
            summary["stats_rows"] += 1

        summary["groups"].append({"group": group.group_name, "stage": group.stage.stage_name,
                                  "kind": sheet["kind"], "rows": len(sheet["rows"])})

    # ── FAIL-SAFE on the FIRST import only (owner 2026-08-21) ────────────────────────────────
    # Rankings and tier are deliberately NOT new fields on Event: rankings is
    # afc_rankings.EventCountingControl.counts_toward_rankings and tier is Event.tournament_tier.
    # The problem was the DEFAULT. EventCountingControl documents "no row for an event => everything
    # counts", and nothing here created a row, so an imported event reached the official ladder
    # unless somebody remembered to switch it off. That is the opposite of the fail-safe the two
    # profile switches use, and it moves REAL teams up and down a public ranking.
    #
    # Tier matters for the same reason and is easy to miss: aggregation passes tier as the WEIGHT
    # applied to an event's results, and auto_classify_event derives it from the PRIZE POOL. An
    # imported event's prize pool is whatever the admin happened to type, not the real one, so
    # leaving the classifier free to run lets a number nobody imported scale everybody's points.
    # tier_overridden=True pins it, exactly as a manual admin tier does.
    #
    # FIRST import only, keyed off results_imported_at being unset BEFORE this block. A re-import
    # must not silently undo an admin who has since decided this event should count.
    first_import = event.results_imported_at is None
    if first_import:
        from afc_rankings.models import EventCountingControl
        EventCountingControl.objects.get_or_create(
            event=event,
            defaults={"counts_toward_rankings": False, "updated_by": actor},
        )
        if not event.tier_overridden:
            event.tier_overridden = True

    # Mark the event as carrying imported results. NOT event_type="external", which already means
    # "registration happens off-platform" and would surface a Register button (spec section 4.3).
    event.results_imported_at = timezone.now()
    event.results_imported_by = actor
    event.save(update_fields=["results_imported_at", "results_imported_by", "tier_overridden"])
    summary["rankings_defaulted_off"] = first_import

    imp.status = "committed"
    imp.summary = summary
    imp.save(update_fields=["status", "summary"])
    return summary
