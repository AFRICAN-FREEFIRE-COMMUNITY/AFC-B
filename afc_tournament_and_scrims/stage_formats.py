"""
What KIND of stage is this? (owner backlog item 21, built 2026-08-13)

WHY THIS EXISTS: `Stages.stage_format` has accumulated three generations of values, and the code
asking "is this Clash Squad?" had drifted into three different literals -
`== "cs - knockout"`, `.startswith("cs - ")` and `.startswith("cs")` - in views.py, the admin page
and seeding_management. Adding the short `"cs"` / `"br"` values for the two-question stage picker
would have made a fourth. One module answers the question instead, so the next value costs one edit
here rather than a grep across the app.

THE THREE GENERATIONS
  1. `"br - normal"`, `"br - roundrobin"`      the originals
  2. `"cs - knockout"`, `"br - round robin"`   game + mode baked into one string
  3. `"cs"`, `"br"`                            game only; the MODE now lives per group on
                                               StageGroups.bracket_format

All three still work and still mean the same thing. Generation 3 is what the picker writes now.

DELIBERATELY DEPENDENCY-FREE - it imports nothing, so anything (models, views, seeding, the
engine) can use it without risking an import cycle.
"""

# The bracket engine each legacy Clash Squad format maps onto. Generation-3 stages carry no mode
# here at all: their mode is on the group, which is the entire point of the change.
LEGACY_CS_MODE = {
    "cs - knockout": "single_elim",
    "cs - double elimination": "double_elim",
    "cs - league": "league",
    "cs - round robin": "round_robin_h2h",
    # A "normal" Clash Squad stage was always run as a straight knockout.
    "cs - normal": "single_elim",
}


def is_clash_squad(stage_format) -> bool:
    """True for every Clash Squad stage, of any generation.

    Matches `"cs"` exactly and anything starting `"cs - "`. Written as those two cases rather than
    a bare `startswith("cs")` so a future format like "cshowmatch" cannot be swept in by accident.
    """
    value = str(stage_format or "").strip().lower()
    return value == "cs" or value.startswith("cs - ")


def is_battle_royale(stage_format) -> bool:
    """True for every Battle Royale stage, of any generation."""
    value = str(stage_format or "").strip().lower()
    return value == "br" or value.startswith("br - ") or value == "br - roundrobin"


def legacy_bracket_mode(stage_format):
    """The bracket engine a GENERATION-2 Clash Squad stage implied, or None.

    Returns None for `"cs"`, because a generation-3 stage has no stage-level mode: ask the group.
    Used by the data migration to move each old stage's mode down onto its group, and by the
    endpoints as a fallback when a caller sends no explicit format.
    """
    return LEGACY_CS_MODE.get(str(stage_format or "").strip().lower())
