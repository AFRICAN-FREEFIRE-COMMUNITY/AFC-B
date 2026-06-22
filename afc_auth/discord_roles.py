"""
afc_auth.discord_roles — duplicate-safe queueing for DiscordRoleAssignment rows.

WHY THIS EXISTS (bug "failed to start", 2026-06-12)
    DiscordRoleAssignment has NO unique constraint on its natural key
    (user, role_id, stage, group) - and it CANNOT get a useful one on MySQL, because
    stage/group are nullable and MySQL treats every NULL as distinct inside a unique
    index (the group=None stage-role rows, the exact shape that crashed event start,
    would never be protected). bulk_create(ignore_conflicts=True) is therefore a no-op
    for duplicates: every re-registration / re-seed quietly inserted another copy, until
    reconcile_stage_roles' get_or_create met 5 copies of one row and 500'd.

    Enforcement lives HERE instead: every bulk writer routes through
    queue_discord_role_assignments(), which set-differences the candidates against the
    existing tuples in ONE query and only inserts the truly missing rows.

HOW IT CONNECTS
    - Consumers: afc_tournament_and_scrims.views (register_for_event team paths, the
      stage/group seeding bulk writers).
    - The one-off data repair for already-accumulated duplicates is the management
      command afc_auth/management/commands/dedupe_discord_role_assignments.py; run it
      once per environment (prod: before anything else at deploy).
"""
from .models import DiscordRoleAssignment


def queue_discord_role_assignments(candidates, batch_size=500):
    """bulk_create only the candidates whose (user, role_id, stage, group) tuple does
    not already have a row - ANY status counts as existing (a failed row is retried by
    the retry endpoint, not by inserting a twin). Returns the number actually created.

    `candidates` are unsaved DiscordRoleAssignment instances. One query sizes the
    existing set (scoped to the candidates' users + roles, so the probe stays small),
    then plain set membership filters the inserts."""
    candidates = [c for c in candidates if c is not None]
    # Drop candidates with no discord_id (owner 2026-06-22 registration-500 fix): a user who has
    # NOT connected Discord has discord_id=None, but DiscordRoleAssignment.discord_id is NOT NULL,
    # so including them crashed bulk_create with IntegrityError("Column 'discord_id' cannot be
    # null") - the generic "An error occurred" a player hit on "Continue to Discord" once Discord
    # became optional on the frontend. Such users simply get no role queued; if they connect
    # Discord later, the reconcile path re-adds it.
    candidates = [c for c in candidates if getattr(c, "discord_id", None)]
    if not candidates:
        return 0

    user_ids = {c.user_id for c in candidates}
    role_ids = {c.role_id for c in candidates}
    existing = set(
        DiscordRoleAssignment.objects.filter(
            user_id__in=user_ids, role_id__in=role_ids,
        ).values_list("user_id", "role_id", "stage_id", "group_id")
    )

    fresh, seen = [], set()
    for c in candidates:
        key = (c.user_id, c.role_id, c.stage_id, c.group_id)
        # De-dupe inside the batch too (a roster can list a user once per role only).
        if key in existing or key in seen:
            continue
        seen.add(key)
        fresh.append(c)

    DiscordRoleAssignment.objects.bulk_create(fresh, batch_size=batch_size)
    return len(fresh)
