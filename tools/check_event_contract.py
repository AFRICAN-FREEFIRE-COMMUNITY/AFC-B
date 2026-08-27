"""Fail when the Event model and the event contract have drifted apart.

WHY A SCRIPT AS WELL AS A TEST
    The test catches drift in CI. This catches it in a pre-commit hook, or by hand, and prints the
    field names rather than a stack trace. Written in the spirit of GROW-APP/tools/check-worklog.py,
    which checks the register against the disk BOTH ways.

WHAT IT ASKS
    1. Is every Event column accounted for? Declared in EVENT_FIELDS, exposed through a derived
       key, or named as internal. NOT "is every column exposed": plenty are correctly invisible.
       The point is that a NEW column cannot be half-added and forgotten.
    2. Is any output key declared twice? Two rows with the same name means one silently wins.
    3. Does every declared field point at something real? A `source=` typo would otherwise only
       surface as an AttributeError at request time.
    4. Is every column a duplicate does not inherit named deliberately? DUPLICATE_EXCLUDED is the
       single decision point, and this prints what it currently drops so the list can be read.

WHY THIS EXISTS AT ALL
    duplicate_event dropped seven configuration fields, including the Discord registration gate,
    for an unknown length of time. Nothing failed, because a missing keyword argument just takes
    the column default. Prose in a rule did not stop that. A check that fails will.

Run: python tools/check_event_contract.py
Exits 1 and names every problem.
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.settings")
django.setup()

from afc_tournament_and_scrims import event_contract as ec  # noqa: E402
from afc_tournament_and_scrims.models import Event  # noqa: E402

problems = []

# 1. Every column accounted for.
for name in ec.unaccounted_fields():
    problems.append(
        f"UNACCOUNTED: Event.{name} is in none of EVENT_FIELDS, DERIVED_FROM, INTERNAL_FIELDS"
    )

# 2. No output key declared twice.
names = [f.name for f in ec.EVENT_FIELDS]
for name in sorted({n for n in names if names.count(n) > 1}):
    problems.append(f"DUPLICATE: '{name}' is declared more than once in EVENT_FIELDS")

# 3. Every declared field points at something real. A field with a `get` computes its value and
#    does not need a column; everything else must RESOLVE on the model. Resolving means one of
#    three things, and all three are legitimate: a column name, a foreign key's attname
#    (organization_id), or a property on the class (roster_edit_open, which derives from
#    roster_edit_until versus now). Checking only column names reported both of those as dangling
#    the first time this ran.
model_fields = {f.name for f in Event._meta.concrete_fields}
resolvable = (
    model_fields
    | {f.attname for f in Event._meta.concrete_fields}
    | {name for name in dir(Event) if not name.startswith("_")}
)
for field in ec.EVENT_FIELDS:
    if field.get is None and field.attr not in resolvable:
        problems.append(
            f"DANGLING: '{field.name}' reads Event.{field.attr}, which is not a column "
            "(a source= typo, or a field that needs a get=)"
        )

# 4. The admin subsets may only name fields that exist in the contract.
for label, subset in (("ADMIN_OVERVIEW_FIELDS", ec.ADMIN_OVERVIEW_FIELDS),
                      ("ADMIN_TIMELINE_FIELDS", ec.ADMIN_TIMELINE_FIELDS)):
    for name in subset:
        if name not in names:
            problems.append(f"UNKNOWN: {label} names '{name}', which is not declared")

# 5. DUPLICATE_EXCLUDED may only name real columns, or it is silently excluding nothing.
for name in sorted(ec.DUPLICATE_EXCLUDED - model_fields):
    problems.append(
        f"STALE: DUPLICATE_EXCLUDED names '{name}', which is not an Event column, "
        "so it excludes nothing"
    )

for line in problems:
    print(line)

if problems:
    print(
        f"\n{len(problems)} problem(s). See the hard rule \"One contract per domain object\" in "
        "WEBSITE/CLAUDE.md."
    )
    sys.exit(1)

carried = ec.duplicate_field_names()
print(
    f"OK. {len(model_fields)} Event columns: {len({f.attr for f in ec.EVENT_FIELDS})} declared, "
    f"{len(ec.DERIVED_FROM)} exposed through derived keys, {len(ec.INTERNAL_FIELDS)} internal."
)
print(f"    {len(ec.EVENT_FIELDS)} output keys. A duplicate inherits {len(carried)} columns "
      f"and deliberately drops {len(ec.DUPLICATE_EXCLUDED)}.")
