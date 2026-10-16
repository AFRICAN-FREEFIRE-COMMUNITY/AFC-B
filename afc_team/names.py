"""
afc_team/names.py - what a team name must be, decided in one place (inbox #276, 2026-10-16).

WHY: a team was created as "." (and another as "---"). Team pages are addressed by the NAME
(/teams/<name>), and "." is not an address at all: a browser folds /teams/. into /teams, so the team
had no page, and a name with no letter or number tells nobody anything in a standings table.

THE RULE
  - at least one letter or number, in ANY alphabet. Free Fire players write names with styled
    characters (fullwidth ＧＨＯＳＴ, mathematical bold 𝙉𝙀𝙓𝙐𝙎, small caps ᴀʜ, CJK 丅乄), and all of
    those are letters to Python's str.isalnum(), so they pass.
  - the invisible Hangul fillers do NOT count: they are category Lo (a "letter") but draw nothing,
    and players use them for blank-looking names. Without this, "ㅤㅤ" would pass.
  - not the name of a page that lives under /teams/ (frontend app/(user)/teams/<segment>): a team
    called "create" would be shadowed by the create-team page.

CALLERS: afc_team.views.create_team, and afc_team.views.edit_team when the name CHANGES (a team that
already has an old name can still edit its description without being forced to rename). Existing
names are untouched; the two teams that break the rule are renamed by hand on the owner's word.
Frontend: the refusal codes are translated in messages/<loc>/team.json (nameRule.*).
"""

# U+115F and U+1160 (Hangul choseong / jungseong fillers), U+3164 (Hangul filler, the one players
# paste), U+FFA0 (its halfwidth form). All draw nothing.
INVISIBLE_LETTERS = frozenset("ᅟᅠㅤﾠ")

# Folders under app/(user)/teams/ that are pages, not teams. Compared case-insensitively.
RESERVED_NAMES = frozenset({"create"})


def team_name_problem(name):
    """Return the refusal code for a team name that cannot be used, or None when it is fine.

    `name` is the name as sent; surrounding spaces are ignored here because Team.save strips them.
    An empty name is the caller's own `team_name_required` check, but is refused here too.
    """
    cleaned = (name or "").strip()
    if not any(ch.isalnum() and ch not in INVISIBLE_LETTERS for ch in cleaned):
        return "team_name_needs_letter"
    if cleaned.casefold() in RESERVED_NAMES:
        return "team_name_reserved"
    return None


# The English sentence each code answers with (the frontend shows its own translation by code).
MESSAGES = {
    "team_name_needs_letter": "A team name needs at least one letter or number.",
    "team_name_reserved": "That name belongs to a page on AFC. Please choose another name.",
}
