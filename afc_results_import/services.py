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

from afc_rankings.models import GhostTeam
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Match, StageCompetitor, StageGroupCompetitor, Stages, StageGroups,
    TournamentTeam, TournamentTeamMatchStats,
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
    """
    want = norm_team_name(sheet_name)
    groups = (StageGroups.objects
              .filter(stage__event=event)
              .select_related("stage"))
    for g in groups:
        combined = norm_team_name(f"{g.stage.stage_name} {g.group_name}")
        if want in (norm_team_name(g.group_name), combined):
            return g
        if want and (want == combined or want.endswith(norm_team_name(g.group_name))):
            if stage_hint is None or norm_team_name(stage_hint) in combined:
                return g
    return None


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

    # Mark the event as carrying imported results. NOT event_type="external", which already means
    # "registration happens off-platform" and would surface a Register button (spec section 4.3).
    event.results_imported_at = timezone.now()
    event.results_imported_by = actor
    event.save(update_fields=["results_imported_at", "results_imported_by"])

    imp.status = "committed"
    imp.summary = summary
    imp.save(update_fields=["status", "summary"])
    return summary
