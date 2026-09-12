"""group_capacity.py - the ONE place that decides which group a competitor lands in.

Owner 2026-09-12: "the groups are not decided by the mechanism, but by what was set in the
structure, how many per group and how many groups". Until now a stage stored only
`number_of_groups`, and five separate splits each did `groups[index % len(groups)]`:

    views.seed_stage_competitors_to_groups          the solo random seeder
    views.seed_stage_competitors_to_groups_team     the team random seeder
    seeding_management._distribute_into_groups      autoseed on registration, reseed, after a
                                                    group delete
    seeding_management.seed_next_stage_by_standings the round-robin snake into the next stage
    afc_draws.services.deal / _extra_card           the sealed card draw

`Stages.competitors_per_group` is the second half of the structure. Every split above now asks
this module, so the rule lives once:

  * size None (the default, every stage created before today): no cap, an even split, exactly
    what the modulo did.
  * size N with g groups: the stage holds g x N. A pool that fits is spread least-loaded-first
    (on an empty stage that IS round robin, so nothing changes for a stage that fits). A pool
    that does not fit is REFUSED with a sentence naming the numbers, never trimmed silently,
    except by the best-effort autoseed, which places what fits and reports the rest (it runs
    inside a registration and must not fail it).

Connects to: Stages.competitors_per_group (models.py), the five callers above, the frontend
stage modals that set the size (StageModal / StageConfigModal) and the public Structure tab
and draw board that display it.
"""
from __future__ import annotations

from collections import Counter

from .models import StageGroupCompetitor


class GroupCapacityError(Exception):
    """A pool larger than the stage's groups x size. The message is written for the organizer's
    toast, so callers pass it through unchanged (HTTP 400 in the seeders, DrawError in the draw)."""


def stage_size(stage):
    """The configured size, or None when the stage has no fixed group size."""
    size = getattr(stage, "competitors_per_group", None)
    return int(size) if size else None


def stage_capacity(stage, groups):
    """groups x size, or None when there is no fixed size."""
    size = stage_size(stage)
    return size * len(groups) if size else None


def current_counts(groups):
    """{group_id: rows already in that group}, one query for the whole stage."""
    ids = [g.group_id for g in groups]
    if not ids:
        return {}
    counts = Counter(
        StageGroupCompetitor.objects.filter(stage_group_id__in=ids).values_list("stage_group_id", flat=True)
    )
    return {gid: counts.get(gid, 0) for gid in ids}


def refusal(stage, groups, wanted, room):
    """The sentence an organizer reads when a pool does not fit."""
    size = stage_size(stage)
    label = "players" if getattr(stage.event, "participant_type", "") == "solo" else "teams"
    return (
        f"{wanted} {label} for {len(groups)} groups of {size}: room for {room}. "
        f"Add a group or raise the {label} per group on the stage."
    )


def check_fits(stage, groups, wanted, counts=None):
    """Raise GroupCapacityError when `wanted` more competitors cannot all be placed."""
    size = stage_size(stage)
    if not size:
        return
    counts = counts if counts is not None else current_counts(groups)
    room = sum(max(0, size - counts.get(g.group_id, 0)) for g in groups)
    if wanted > room:
        raise GroupCapacityError(refusal(stage, groups, wanted, room))


def plan_placements(stage, groups, count, counts=None, strict=True):
    """The group for each of `count` competitors, in order, as a list of groups.

    Least-loaded-first over the current row counts, ties broken by the groups' order (the callers
    pass them ordered by group_id, as they always did), so on an empty stage this is the same
    round robin as before. With a size it never places past it: strict callers get
    GroupCapacityError up front, autoseed (strict=False) gets a shorter list and places what fits.
    """
    if not groups:
        return []
    size = stage_size(stage)
    counts = dict(counts if counts is not None else current_counts(groups))
    if size and strict:
        check_fits(stage, groups, count, counts)
    order = {g.group_id: i for i, g in enumerate(groups)}
    plan = []
    for _ in range(count):
        open_groups = [g for g in groups if not size or counts.get(g.group_id, 0) < size]
        if not open_groups:
            break
        target = min(open_groups, key=lambda g: (counts.get(g.group_id, 0), order[g.group_id]))
        counts[target.group_id] = counts.get(target.group_id, 0) + 1
        plan.append(target)
    return plan
