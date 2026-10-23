"""
tools/idor_cases_generate.py - one ID-guessing case per id-taking address (owner rule R88, inbox #101).

Owner, 2026-09-30, on the 229 addresses that take an id and had no IDOR case: "fix them."

WHAT IT WRITES
  security/idor-cases.json, keeping every hand-written case and replacing the generated ones
  (marked "generated": true). For each address Django resolves whose path takes an id or a slug:
    - the real id / slug of an existing record, read from the database this runs against (the
      scratch or walk copy of production), so the probe asks about something that exists;
    - the first method the view allows (GET when it allows GET);
    - who tries: user "b", a signed-in ordinary player with no roles and no team;
    - what must come back: a refusal (401 / 403 / 404 / 405). An address public by design (the
      reviewed list in tools/anonymous_answers.json) expects 200 on GET and skips the probes that
      only make sense for private records.
  The R88 runtime probe (check-security --idor) then tries every case on GET, PUT, PATCH and DELETE,
  with no session, on the neighbouring ids, and a real id against a missing one.

  An address that is legitimately open to ANY signed-in player is not a hole. Those are named in
  tools/idor_member_reads.json with the answer they give an ordinary player and the reason.

RUN (against a copy of production, never production itself):
    python manage.py shell -c "exec(open('tools/idor_cases_generate.py').read())"
"""
import json
import re
from pathlib import Path

from django.apps import apps
from django.urls import URLPattern, URLResolver, get_resolver

ROOT = Path.cwd()  # run from the repo root (exec() has no reliable __file__)
CASES = ROOT / "security" / "idor-cases.json"
PUBLIC = json.loads((ROOT / "tools" / "anonymous_answers.json").read_text(encoding="utf8"))["public"]
MEMBER_READS_FILE = ROOT / "tools" / "idor_member_reads.json"
MEMBER_READS = json.loads(MEMBER_READS_FILE.read_text(encoding="utf8"))["reads"] if MEMBER_READS_FILE.exists() else {}

ID_NAME = re.compile(r"id|slug|pk|uuid|handle", re.I)
CAPABILITY = re.compile(r"token|signature|sig$|hmac", re.I)
PARAM = re.compile(r"<(?:(\w+):)?(\w+)>")


def walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


def model(label):
    try:
        return apps.get_model(label)
    except LookupError:
        return None


def first(label, field="pk", **filters):
    m = model(label)
    if m is None:
        return None
    try:
        return m.objects.filter(**filters).order_by("-pk").values_list(field, flat=True).first()
    except Exception:
        return None


# (app, param) -> where a real value lives. App-specific first, then by name alone.
RESOLVE = {
    ("afc_organizers", "slug"): lambda: first("afc_organizers.Organization", "slug"),
    ("afc_polls", "slug"): lambda: first("afc_polls.Poll", "slug"),
    ("afc_referrals", "slug"): lambda: first("afc_referrals.ReferralProgram", "slug"),
    ("afc_auth", "report_id"): lambda: first("afc_auth.UserReport"),
    ("afc_organizers", "report_id"): lambda: first("afc_organizers.OrganizationReport"),
    ("afc_player_market", "report_id"): lambda: first("afc_player_market.MarketReport"),
    ("afc_sponsors", "submission_id"): lambda: first("afc_sponsors.SponsorEngagementSubmission"),
    ("afc_tournament_and_scrims", "submission_id"): lambda: first("afc_tournament_and_scrims.TeamMapResultSubmission"),
    ("afc_feedback", "submission_id"): lambda: first("afc_feedback.FeedbackSubmission"),
    ("afc_partner_apply", "application_id"): lambda: first("afc_partner_apply.PartnerApplication"),
    ("afc_sso", "application_id"): lambda: first("afc_sso.AFCSSOApplication"),
    ("afc_sponsors", "invite_id"): lambda: first("afc_sponsors.SponsorMemberInvite"),
    ("afc_team", "invite_id"): lambda: first("afc_team.Invite", "invite_id"),
    ("afc_tournament_and_scrims", "sponsor_id"): lambda: first("afc_sponsors.Sponsor"),
}
BY_NAME = {
    "event_id": lambda: first("afc_tournament_and_scrims.Event", "event_id"),
    "stage_id": lambda: first("afc_tournament_and_scrims.Stages", "stage_id"),
    "match_id": lambda: first("afc_tournament_and_scrims.Match", "match_id"),
    "mid": lambda: first("afc_leaderboard.ParticipantMatchResult", "match_id"),
    "pid": lambda: first("afc_leaderboard.LeaderboardParticipant"),
    "lb_id": lambda: first("afc_leaderboard.StandaloneLeaderboard"),
    "job_id": lambda: first("afc_leaderboard.LeaderboardOcrJob", "pk"),
    "session_id": lambda: first("afc_ocr.OCRSession", "pk"),
    "draw_id": lambda: first("afc_draws.StageDraw"),
    "user_id": lambda: first("afc_auth.User", "user_id", is_active=True),
    "player_id": lambda: first("afc_auth.User", "user_id", is_active=True),
    "team_id": lambda: first("afc_team.Team", "team_id"),
    "season_id": lambda: first("afc_rankings.Season", "season_id"),
    "ghost_team_id": lambda: first("afc_rankings.GhostTeam", "pk"),
    "sponsor_id": lambda: first("afc_sponsors.Sponsor"),
    "member_id": lambda: first("afc_sponsors.SponsorMember"),
    "attachment_id": lambda: first("afc_support.SupportAttachment"),
    "order_id": lambda: first("afc_shop.Order", "order_id"),
    "watch_id": lambda: first("afc_auth.WatchlistEntry"),
    "org_id": lambda: first("afc_organizers.Organization"),
    "design_id": lambda: first("afc_organizers.OrgLeaderboardDesign"),
    "font_id": lambda: first("afc_organizers.OrgLeaderboardDesignFont"),
    "blacklist_id": lambda: first("afc_organizers.OrganizerBlacklist"),
    "field_id": lambda: first("afc_organizers.OrgLeaderboardDesignField"),
    "logo_id": lambda: first("afc_organizers.OrgLeaderboardDesignLogo"),
    "page_id": lambda: first("afc_organizers.OrgLeaderboardDesignPage"),
    "text_id": lambda: first("afc_organizers.OrgLeaderboardDesignText"),
    "request_id": lambda: first("afc_organizers.BlacklistLiftRequest"),
    "rule_id": lambda: first("afc_rankings.EventTierRule"),
    "exclusion_id": lambda: first("afc_rankings.ResultExclusion"),
    "payout_id": lambda: first("afc_tournament_and_scrims.EventPrizePayout"),
    "preset_id": lambda: first("afc_tournament_and_scrims.CSRoomPreset"),
    "link_id": lambda: first("afc_tournament_and_scrims.EventLink"),
    "waiver_id": lambda: first("afc_tournament_and_scrims.EventRequirementWaiver"),
    "campaign_id": lambda: first("afc_tournament_and_scrims.EventInvitationCampaign"),
    "invitation_id": lambda: first("afc_tournament_and_scrims.EventTeamInvitation"),
    "flag_id": lambda: first("afc_tournament_and_scrims.MediaFlag"),
    "overlay_id": lambda: first("afc_tournament_and_scrims.EventOverlay"),
    "pending_id": lambda: first("afc_tournament_and_scrims.PendingCaptureUpload"),
    "key_id": lambda: first("afc_partner_api.PartnerApiKey"),
    "event_slug": lambda: first("afc_tournament_and_scrims.Event", "slug"),
    "provider_slug": lambda: "google",
    "scope": lambda: "event",
    "object_id": lambda: first("afc_tournament_and_scrims.Event", "event_id"),
}


def resolve_value(app, name, conv):
    fn = RESOLVE.get((app, name)) or BY_NAME.get(name)
    value = fn() if fn else None
    if value is None:
        value = "00000000-0000-0000-0000-000000000000" if conv == "uuid" else ("no-such-slug" if conv in ("slug", "str") else 999999)
    return value


def methods_of(callback):
    cls = getattr(callback, "cls", None)
    names = [m.upper() for m in getattr(cls, "http_method_names", []) if m not in ("options", "head", "trace")]
    return names or ["GET"]


def app_of(callback):
    mod = getattr(callback, "__module__", "") or ""
    return mod.split(".")[0]


def build():
    generated = []
    for route, cb in walk(get_resolver().url_patterns):
        params = PARAM.findall(route)
        names = [n for _c, n in params]
        if not params or not any(ID_NAME.search(n) for n in names) or any(CAPABILITY.search(n) for n in names):
            continue
        app = app_of(cb)
        if not app.startswith("afc_"):
            continue
        path = "/" + route
        for conv, name in params:
            path = path.replace(f"<{conv + ':' if conv else ''}{name}>", str(resolve_value(app, name, conv)), 1)
        ms = methods_of(cb)
        method = "GET" if "GET" in ms else ms[0]
        key = f"{method} {route}"
        case = {"name": f"R88 generated: {key}", "generated": True, "route": route,
                "attempt": {"as": "b", "method": method, "path": path, "expect": [401, 403, 404, 405]}}
        if key in PUBLIC:
            case["attempt"]["expect"] = [200]
            case["r88"] = {"skip": ["anon", "neighbours", "existence"],
                           "reason": f"public by design: {PUBLIC[key].get('reason', 'tools/anonymous_answers.json')}"}
        elif key in MEMBER_READS:
            # Open to ANY signed-in player by design (reviewed one by one, tools/idor_member_reads.json):
            # what it must answer the ordinary player, and why. The probes that still apply run.
            entry = MEMBER_READS[key]
            case["attempt"]["expect"] = entry["expect"]
            case["r88"] = {"skip": entry.get("skip", ["existence"]), "reason": entry["reason"]}
        generated.append(case)
    data = json.loads(CASES.read_text(encoding="utf8"))
    kept = [c for c in data["cases"] if not c.get("generated")]
    data["cases"] = kept + sorted(generated, key=lambda c: c["name"])
    CASES.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    print(f"hand-written {len(kept)}, generated {len(generated)}")


build()
