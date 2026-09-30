# afc_auth/site_paths.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SITE ADDRESSES THAT CARRY A NAME (owner 2026-09-30, inbox #91).
#
# Player, team and sponsor pages on the website are addressed by the name itself (owner rule R22),
# and a name is typed by a person: spaces, "#", "?", "/", "%", accents, Free Fire glyphs (NG.KILLA
# ends in U+F8FF). Every such name must be percent-encoded as ONE path segment, "/" included, or
# "A/B" becomes two segments and "A#1" loses everything after the "#". urllib's quote() leaves "/"
# alone by default, which is how afc_qr/targets.py got it half right.
#
# This is the backend twin of the frontend's lib/routes.ts (segment / playerPath / teamPath ...):
# both sides encode the same way, and the frontend decodes with readSegment(). Callers:
#   afc_auth/notification_links.py   "Take me there" links on notifications
#   afc_polls/hydration.py           profile_url on poll options
#   afc_qr/targets.py                the page a QR code opens
#   afc_referrals/views.py           the referral link path
#   afc_player/views.py              moved_to on the admin player page
#   afc_player_market/views.py, afc_tournament_and_scrims/event_invite_delivery.py  emailed links
# ─────────────────────────────────────────────────────────────────────────────────────────────────
from urllib.parse import quote


def segment(value):
    """One path segment: every unsafe character percent-encoded, "/" included. None -> ""."""
    return quote("" if value is None else str(value), safe="")


def player_path(username, rest=""):
    """Public player page, by in-game name."""
    return f"/players/{segment(username)}{rest}"


def team_path(team_name, rest=""):
    """Public team page, by team name. `rest` is a sub-path such as "/applications"."""
    return f"/teams/{segment(team_name)}{rest}"


def admin_player_path(username, rest=""):
    """Admin player page, by in-game name."""
    return f"/a/players/{segment(username)}{rest}"


def referral_path(code):
    """Referral landing page, by code."""
    return f"/r/{segment(code)}"
