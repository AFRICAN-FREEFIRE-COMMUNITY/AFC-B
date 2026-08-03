import re
import uuid
from collections import Counter
from functools import lru_cache

# rapidfuzz is imported ONCE here, not per comparison: match_name runs per read player against the
# whole platform pool (~6.8k users), so a per-call import cost is paid hundreds of thousands of times.
# It stays optional - a missing rapidfuzz degrades to "no fuzzy candidates" rather than a hard error.
try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:                                     # pragma: no cover - rapidfuzz is a hard dep
    _fuzz = None

# The SHARED text normalizer ("powerful search", utils/search_utils.py). It folds stylized Free Fire
# letterforms (small-caps ᴀᴇ, Cyrillic/Greek look-alikes, Cherokee, regional-indicator flags), strips
# accents + every non-alphanumeric, lower-cases, and folds look-alike digits (0->o, 1->i, 3->e, 5->s).
# The OCR matchers used to compare RAW strings, so a read that was character-for-character the same
# player after normalization still lost to a shorter neighbour ("ZN.MALX09" -> user "ZN.MALXO9" lost to
# "AL"; "AW.TRAP7" -> user "AW.trap7" lost to "TRAPPIE_APK"). Reusing this one normalizer keeps OCR
# identity, the server typeaheads (afc_team.views.search_teams / afc_auth.views.search_users) and the
# browser-side list filters (frontend/lib/search.ts) all agreeing on when two names are "the same".
from utils.search_utils import normalize_search_text

CONFIDENCE_AUTO = 0.85
CONFIDENCE_WARN = 0.75

# ── short-read guard ────────────────────────────────────────────────────────────────────────────
# A Free Fire screenshot often shows only a 2-4 character clan TAG where a full team name belongs.
# rapidfuzz's WRatio rewards a short query for merely being *containable* in a long candidate, so
# those short reads scored 90-100 against completely unrelated teams on real prod data:
#   "ST" -> "Satolas" 1.00 | "XIT" -> "QX4" 0.90 | "FXP" -> "FLUXOmz" 0.90 | "R3D" -> "AETHELGARD" 0.72
# and a trailing dot was enough to flip a winner ("NXT." scored "QX4" 0.90 OVER the correct
# "NXT ESP" 0.86). A short read is only real evidence when it OPENS the candidate (a tag prefix,
# "NXT" -> "NXT ESP") or equals it outright; anything else is a coincidence and must never
# auto-resolve. We still LIST those candidates (recall is preserved, the reviewer can pick one) but
# cap their score under the review gate so nothing binds a standings row to the wrong team silently.
SHORT_READ_LEN = 5        # a normalized string shorter than this is treated as a tag, not a name
TAG_MIN_LEN = 2           # below this, only exact equality can match: one letter identifies nobody
TAG_PREFIX_MIN_LEN = 3    # a prefix hit is only a real signal from 3 characters up ("NXT" -> "NXT ESP")
SHORT_PREFIX_CAP = 88     # tag that opens the candidate: strong, but never an exact-identity 100
SHORT_OTHER_CAP = 55      # tag that merely resembles it: a visible candidate, below the floor below

# Two DIFFERENT thresholds, on purpose:
#   CANDIDATE_CUTOFF - what we LIST. Deliberately loose (owner 2026-06-11: "if there are similar
#       names on the platform, it lists them") so the reviewer always has the near misses to pick from.
#   MATCH_FLOOR      - what we ASSERT. Below this we return NO matched id at all: the row comes back
#       is_unmatched with its candidates attached, so it surfaces for a human instead of arriving
#       pre-bound to whatever scored highest. A wrong auto-match corrupts a tournament standing; an
#       unmatched row costs the organizer one click.
# SHORT_OTHER_CAP sits BELOW MATCH_FLOOR by construction, so every "short fragment coincidence"
# ("XIT" vs team "QX4") can only ever be a suggestion, never an assertion.
CANDIDATE_CUTOFF = 30
MATCH_FLOOR = 60
# How many raw-scored candidates the C-level scan hands back before the short-fragment cap is applied
# and the top 5 are kept. Generous headroom, see the PERFORMANCE note in match_name.
SHORTLIST_LIMIT = 50


# ──────────────────────────────────────────────────────────────────────────────
# Shared scoring core, used by BOTH match_name (players) and match_team_name (teams)
# ──────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=100_000)
def _norm_cached(value: str) -> str:
    return normalize_search_text(value)


def _norm(value) -> str:
    """Normalize one name for comparison. Thin alias over the shared normalize_search_text so the
    intent reads clearly at every call site (and so a future tweak has one place to live).

    MEMOIZED because the access pattern is extremely repetitive: match_name is called once per read
    player and walks the ENTIRE platform pool each time, so a 77-player map normalizes the same ~6.8k
    usernames 77 times over (half a million NFKD passes, ~100 ms per read). The cache turns that into
    one pass per distinct string. Keyed on the raw string, so it is correct for any input; usernames
    and team names change rarely and a stale entry can only exist for a string that is byte-identical
    anyway. Bounded at 100k entries so it cannot grow without limit in a long-lived worker.
    """
    if not value:
        return ""
    return _norm_cached(str(value))


def score_names(read_norm: str, candidate_norm: str) -> float:
    """Score ONE (read, candidate) pair, 0-100, both sides ALREADY normalized by _norm.

    Why normalized-only (no raw comparison): the raw strings are exactly what was broken. Comparing
    "NXT." to "QX4" raw scores 90 on punctuation-and-case noise; comparing "nxt" to "qx4" scores ~33,
    which is the truth. Every legitimate raw win is preserved by normalization instead, because the
    normalizer folds the decoration rather than the identity.

    Ladder:
      1. Identical after normalization -> 100. This is the fix for the whole class of misses where a
         read IS the user, spelled with decoration ("NP.BLOOD彡" == user "NP. BLOOD").
      2. Otherwise rapidfuzz WRatio on the normalized pair.
      3. A SHORT read (a clan tag) is capped, see the SHORT_READ_LEN block above.

    Returns 0.0 when rapidfuzz is unavailable or either side normalizes to empty (a name made only of
    symbols carries no identity and must never match anything).
    """
    if not read_norm or not candidate_norm:
        return 0.0
    if read_norm == candidate_norm:
        return 100.0
    if _fuzz is None:
        return 0.0

    score = float(_fuzz.WRatio(read_norm, candidate_norm))

    # Short-fragment guard (see SHORT_READ_LEN). It keys off the SHORTER of the two strings, not the
    # read: a 1-2 character team_tag on the CANDIDATE side inflates the score exactly as badly as a
    # short read does. On real clone data the read "XIT" scored 0.90 against team "QX4" purely
    # because QX4's tag is the single letter "X", and "FXP" scored 0.90 against "FLUXOmz" on its tag
    # "FX". Neither is evidence of anything.
    shorter = min(len(read_norm), len(candidate_norm))
    if shorter < SHORT_READ_LEN:
        if shorter < TAG_MIN_LEN:
            # One character identifies nobody. Only exact equality (handled above) can match here.
            return 0.0
        # The prefix boost is DIRECTIONAL and only applies when the READ is the short one, i.e. the
        # screenshot showed a tag and the platform record is the full name ("NXT" -> "NXT ESP").
        # The reverse - a short platform record sitting inside a longer read - is NOT the same
        # evidence, because short usernames are common and land inside all sorts of reads: the user
        # "SAL" is a prefix of the read "salttos" but is not that player. Capping that direction at
        # SHORT_OTHER_CAP keeps it visible as a candidate while holding it under the review gate.
        read_is_the_short_one = len(read_norm) < len(candidate_norm)
        if read_is_the_short_one and candidate_norm.startswith(read_norm) and shorter >= TAG_PREFIX_MIN_LEN:
            score = min(score, SHORT_PREFIX_CAP)
        else:
            score = min(score, SHORT_OTHER_CAP)
    return score


def _exact_norm_hits(read_norm: str, candidates: list, key) -> list:
    """Every candidate whose `key` normalizes to exactly `read_norm`.

    Used to give normalized identity absolute priority over fuzzy neighbours, AND to detect the
    ambiguous case: when TWO platform records normalize the same ("KILLUA" and "K1LLUA"), there is no
    right answer to guess, so the caller must surface both for review instead of auto-resolving one.
    Mirrors the collision rule already used by the match-log importer's _name_lookup
    (afc_tournament_and_scrims.views), so both import paths behave the same way on a name clash.
    """
    if not read_norm:
        return []
    return [c for c in candidates if _norm(key(c)) == read_norm]


def get_registered_players(match, event, event_type: str) -> list:
    """
    Returns all players registered in the event.
    Format: [{"user_id", "username", "team_id", "team_name"}]
    """
    from afc_tournament_and_scrims.models import TournamentTeam, RegisteredCompetitors

    players = []

    if event_type == "team":
        teams = (
            TournamentTeam.objects
            .filter(event=event, status="active")
            .select_related("team")
            .prefetch_related("members__user")
        )
        for t_team in teams:
            for member in t_team.members.filter(status__in=["active", "approved"]):
                players.append({
                    "user_id":   member.user_id,
                    "username":  member.user.username,
                    "team_id":   t_team.tournament_team_id,
                    "team_name": t_team.team.team_name,
                })
    else:
        competitors = (
            RegisteredCompetitors.objects
            .filter(event=event, status__in=["registered", "approved"])
            .select_related("user")
        )
        for comp in competitors:
            if comp.user:
                players.append({
                    "user_id":   comp.user_id,
                    "username":  comp.user.username,
                    "team_id":   None,
                    "team_name": None,
                })

    return players


# ──────────────────────────────────────────────────────────────────────────────
# P2: platform-wide candidate pools + a team-name matcher (the standalone-leaderboard
# OCR assist). Unlike get_registered_players / match_name (event flow, roster-gated),
# these match against EVERY team / user on the platform, because a standalone leaderboard
# has no event roster to gate against. Consumed by afc_leaderboard.views.ocr_extract:
#   - team-format LB  -> all_platform_teams() + match_team_name()
#   - solo-format LB  -> all_platform_players() + match_name() (reused as-is)
# match_name (above) is reused unchanged for the solo flow.
# ──────────────────────────────────────────────────────────────────────────────


def all_platform_players(limit=None) -> list:
    """Every registered user as a match candidate (NO roster gate), shaped like
    get_registered_players' rows so match_name can consume the list unchanged.

    Format: [{"user_id", "username", "team_id": None, "team_name": None}]. team_id/team_name are
    always None here: a standalone solo leaderboard carries no team context (a real-user solo
    participant resolves by user_id alone). `limit` caps the pool (paginate huge member bases).
    Read by afc_leaderboard.views.ocr_extract for the solo flow."""
    from afc_auth.models import User

    # Carry each user's CURRENT team (owner 2026-06-12: the review panel must show which team a
    # suggested player is in, not just the username). There is no User.team FK - membership lives
    # on afc_team.TeamMembers (member FK, unique_member_one_team constraint), so the reverse
    # `teammembers` join yields AT MOST one row per user (LEFT JOIN: team fields NULL for free agents).
    qs = (
        User.objects.all()
        .order_by("user_id")
        .values("user_id", "username", "teammembers__team_id", "teammembers__team__team_name")
    )
    if limit is not None:
        qs = qs[:limit]
    return [
        {
            "user_id": u["user_id"],
            "username": u["username"],
            "team_id": u["teammembers__team_id"],
            "team_name": u["teammembers__team__team_name"],
        }
        for u in qs
    ]


def all_platform_teams() -> list:
    """Every real Team as a match candidate for the standalone team flow.

    Format: [{"team_id", "team_name", "team_tag"}]. Read by afc_leaderboard.views.ocr_extract
    (team format) and fed to match_team_name below. No roster gate: a standalone leaderboard can
    feature any team on the platform."""
    from afc_team.models import Team

    return [
        {"team_id": t.team_id, "team_name": t.team_name, "team_tag": t.team_tag}
        for t in Team.objects.all().order_by("team_id")
    ]


def derive_team_tag(player_names: list) -> str:
    """Best-effort TEAM TAG from a placement's player names (owner 2026-06-11: "these team tags can help
    when searching for teams through the tags on the players names").

    Free Fire players wear their clan tag on their IGN, but NOT uniformly: it is a prefix for some
    squads ("NXT.EZIO", "IPW_SUGAR") and a SUFFIX for others ("里克 X FNS", "RENASAR™R"), and one
    teammate always forgets to wear it at all.

    The previous implementation took the leading alphanumeric run of every name and returned their
    os.path.commonprefix, which required the tag to be a PREFIX present on EVERY player. On real prod
    reads that returned "" for most placements:
      ['生活 X FNS', 'IL.PrimeX69', '里克 X FNS', '米奇 X FNS']   -> tag is the SUFFIX "FNS"
      ['KN DANTE F♡', 'KN SABATH24', 'OBINNA', 'AMIRツNE']       -> "KN" on only 2 of 4, commonprefix ""
      ['R3D FIREX77', 'R3D FAMEZYX1', 'MATEツX11TR', 'PINTAX11'] -> "R3D" on only 2 of 4
    and a CJK-leading name ("生活 X FNS") produced no leading run at all.

    So: collect BOTH the first and last token of every name, then take the affix that the PLURALITY of
    players wear. A tag qualifies when it is 2-5 characters, is worn by at least 2 players, and by at
    least half of them - so one bare-named teammate no longer erases the signal, while a single stray
    token can never invent a tag. Ties resolve to the most-worn, then the longest, deterministically.

    Consumed by afc_leaderboard.ocr.build_team_ocr_rows / build_rows_from_match_log, which feed the
    derived tag to match_team_name (whose scorer compares it against each team's team_tag).
    """
    names = [n for n in (player_names or []) if (n or "").strip()]
    if len(names) < 2:
        return ""

    # Tokens split on ANY non-alphanumeric run, so separators (". _ | 乂 ツ ™ ♡ space) all break a name
    # into its parts regardless of script. The first and last token are the only tag positions that
    # occur in practice; a middle token is part of the IGN.
    counts = Counter()
    for raw in names:
        tokens = [t for t in re.split(r"[^0-9A-Za-zÀ-ɏ]+", raw) if t]
        if not tokens:
            continue
        for token in {tokens[0], tokens[-1]}:      # set(): a one-token name votes once, not twice
            tag = _norm(token)
            if 2 <= len(tag) <= 5:
                counts[tag] += 1

    if not counts:
        return ""
    # Most-worn first, then longest, then alphabetical - a stable order so the same read always
    # derives the same tag (the review table must not shuffle between refreshes).
    tag, worn = max(counts.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))
    if worn >= 2 and worn * 2 >= len(names):
        return tag
    return ""


def all_platform_teams_with_ghosts() -> list:
    """The team pool for the STANDALONE OCR flows: every real Team PLUS every GhostTeam.

    Ghost teams join the pool (owner 2026-06-12: attach read players "to a newly created team or old
    ghost team") so a team that was ghost-created on an earlier map/leaderboard surfaces as a match
    SUGGESTION on the next read instead of the admin re-creating a duplicate ghost. Ghost entries are
    shaped like real ones but with team_id=None and a ghost_team_id (str uuid) + is_ghost=True, which
    match_team_name passes through into candidates so the FE can offer "<name> (ghost)" and resolve
    it as kind=ghost_existing. Read by afc_leaderboard.ocr.process_job + views.ocr_extract."""
    from afc_rankings.models import GhostTeam

    pool = all_platform_teams()
    pool.extend(
        {
            "team_id": None,
            "ghost_team_id": str(g["ghost_team_id"]),
            "team_name": g["team_name"],
            "team_tag": None,
            "is_ghost": True,
        }
        for g in GhostTeam.objects.all().values("ghost_team_id", "team_name")
    )
    return pool


def match_team_name(raw_name: str, teams: list) -> dict:
    """The team-format mirror of match_name: fuzzy-match a raw OCR-read team name against the
    platform team pool (from all_platform_teams), returning the best match + top-3 candidates.

    Scoring: the SHARED score_names ladder (normalized identity -> rapidfuzz WRatio on the normalized
    pair -> short-read cap) over BOTH the team_name and the team_tag of each team; a team's score is
    the better of its name-score and tag-score, so a screenshot showing only the short tag ("ALP")
    still resolves at 100 against that team's tag while scoring far lower against unrelated long
    names. Cutoff 30, top-5.

    Returns {row_id, raw_name, matched_team_id, matched_team_name, confidence,
             top_candidates:[{team_id, team_name, confidence}]}. No match -> matched_team_id None,
             matched_team_name None, confidence 0.0, top_candidates []. Consumed by ocr_extract;
             the FE review table renders top_candidates as the per-row dropdown.

    GHOST entries (from all_platform_teams_with_ghosts) carry ghost_team_id/is_ghost; those keys are
    passed through into their candidate dicts, but matched_team_id/matched_team_name (the FE
    auto-resolve) only ever come from the best REAL team - a ghost is always an explicit admin pick,
    never an automatic resolution."""
    row_id = str(uuid.uuid4())

    empty = {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_team_id":   None,
        "matched_team_name": None,
        "confidence":        0.0,
        "top_candidates":    [],
    }
    if not teams:
        return empty

    # Score every team by the better of its name-score and tag-score, then keep the top-5 above the
    # cutoff. We score in Python (not process.extract) because each team has TWO strings (name + tag)
    # to compare and we want the max of the two as that team's confidence. Cutoff 30 + top-5 (owner
    # 2026-06-11: "if there are similar names on the platform, it lists them") - a deliberately LOOSE
    # net so the admin always sees the closest names to pick from, even on a rough read. The best
    # match still drives auto-resolve via the confidence ladder on the FE; the extra candidates are
    # just there to pick.
    read_norm = _norm(raw_name)
    if not read_norm:
        # A team "name" made only of symbols/logo glyphs carries no identity. Matching it would score
        # noise against every team, so we return no match and let the reviewer resolve the row (the
        # players' derived tag / plurality in build_team_ocr_rows is the real signal for these).
        return empty

    scored = []
    for t in teams:
        score = max(
            score_names(read_norm, _norm(t.get("team_name"))),
            score_names(read_norm, _norm(t.get("team_tag"))),
        )
        if score >= CANDIDATE_CUTOFF:
            scored.append((score, t))

    if not scored:
        return empty

    # Deterministic order: score desc, then REAL teams before ghosts (a ghost is never an auto-resolve
    # so it must not occupy the top slot on a tie), then by team name so the same read always produces
    # the same candidate list.
    scored.sort(key=lambda s: (-s[0], bool(s[1].get("is_ghost")), (s[1].get("team_name") or "")))
    top_candidates = []
    for score, t in scored[:5]:
        cand = {
            "team_id": t["team_id"],
            "team_name": t["team_name"],
            "confidence": round(score / 100, 3),
        }
        # Ghost passthrough (see docstring): keep the ghost identity on the candidate so the FE can
        # offer it as a kind=ghost_existing pick.
        if t.get("is_ghost"):
            cand["ghost_team_id"] = t.get("ghost_team_id")
            cand["is_ghost"] = True
        top_candidates.append(cand)

    # Auto-resolve from the best REAL candidate only; ghosts are explicit picks, never automatic.
    # And only when it clears MATCH_FLOOR - a weaker best candidate is still LISTED (the reviewer
    # picks it in one click) but is not asserted as the row's team.
    best_real = next((c for c in top_candidates if not c.get("is_ghost")), None)
    if best_real and best_real["confidence"] * 100 < MATCH_FLOOR:
        best_real = None
    return {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_team_id":   best_real["team_id"] if best_real else None,
        "matched_team_name": best_real["team_name"] if best_real else None,
        "confidence":        best_real["confidence"] if best_real else 0.0,
        "top_candidates":    top_candidates,
    }


def match_name(raw_name: str, registered: list) -> dict:
    """
    1. Check OCRNameAlias for an exact match, honoured ONLY when that aliased user is in
       this event's `registered` roster (confidence = 1.0). OCRNameAlias is a GLOBAL table
       (raw_name unique across all events), so an alias can point at a user who is not in
       the current event; trusting it blindly would auto-resolve a row to an off-event
       player at 1.0 with no candidates (and, on team events, a null team). Roster-gating
       the fast-path keeps aliases event-safe and lets out-of-event aliases fall through
       to the fuzzy pass so `top_candidates` is still surfaced.
    2. NORMALIZED IDENTITY: if exactly one registered username normalizes to the same string as the
       read (shared normalize_search_text - decoration, case, punctuation and look-alike digits all
       folded), that IS the player: confidence 1.0, no fuzzing. If SEVERAL do, the read is ambiguous
       and we fall through on purpose so the reviewer picks rather than the matcher guessing.
    3. Fall back to rapidfuzz against registered usernames, scored on the NORMALIZED pair.
    4. Return top 5 candidates plus the best match row.

    The fuzzy pass scores the read name BOTH as-is AND with a leading team-tag prefix stripped
    ("SYN.ARDNT DS" also scores as "ARDNT DS"), keeping each username's best score. FF screenshots
    prefix IGNs with the team tag, which buried close matches below the cutoff (owner 2026-06-12:
    "it should have had this ARENDT player as part of the options for that ARDNT"). Cutoff 30 +
    top-5 mirrors match_team_name's deliberately LOOSE candidate net - the candidates exist to be
    PICKED from; only the best one drives any auto-resolve.
    """
    from afc_ocr.models import OCRNameAlias

    row_id = str(uuid.uuid4())

    # Step 1: exact alias lookup, roster-gated.
    # OCRNameAlias is global (see docstring). `registered` is this event's roster from
    # get_registered_players (team events carry team_id/team_name; solo events carry None
    # for both). We only honour the alias when the aliased user is actually in that roster,
    # deriving the alias user's team by looking them up inside `registered` (no separate
    # team query lives here). Callers: match_name is invoked per read row by the event OCR
    # commit/draft flow (afc_ocr.services) and reused by afc_leaderboard.views.ocr_extract's
    # solo path (which passes the full platform pool, so a real alias user is still "in").
    alias = (
        OCRNameAlias.objects
        .filter(raw_name__iexact=raw_name)
        .select_related("user")
        .first()
    )
    if alias and alias.user:
        reg = next((p for p in registered if p["user_id"] == alias.user_id), None)
        if reg is not None:                       # roster-gated: only trust an in-event alias
            return {
                "row_id":            row_id,
                "raw_name":          raw_name,
                "matched_user_id":   alias.user_id,
                "matched_username":  alias.user.username,
                "confidence":        1.0,
                "matched_team_id":   reg["team_id"],
                "matched_team_name": reg["team_name"],
                "top_candidates":    [],
            }
        # Alias points at a user NOT registered for this event -> ignore it and fall through
        # to fuzzy matching so the reviewer still sees the real roster candidates instead of
        # an auto-resolved off-event player at confidence 1.0 with no candidates.

    # Step 2: NORMALIZED IDENTITY, before any fuzzing.
    # This is the fix for the owner's "OCR is not matching players" report. On real prod reads, names
    # that are character-for-character the SAME player once decoration is folded were losing to shorter
    # neighbours, because rapidfuzz scores a short username ~90-100 for merely being contained in the
    # read. Every one of these is a real clone-data failure:
    #   "ZN.MALX09" (user "ZN.MALXO9", zero vs letter O)  -> picked "AL"          @0.90
    #   "AW.TRAP7"  (user "AW.trap7",  case only)         -> picked "TRAPPIE_APK" @0.80
    #   "NP.KILLUA" (user "NP. KILLUA", a space)          -> picked "KILLUA"      @1.00 (auto-applied)
    #   "LMG LE00"  (user "LMG   LEOO", zeros + spaces)   -> picked "LE"          @0.90
    #   "NP.BLOOD彡"(user "NP. BLOOD",  a CJK flourish)   -> picked "BLOOD"       @0.91
    #   "NJ Solozin"(user "Nj solozin", case)             -> picked "Solo"        @0.90
    # Normalized identity outranks every fuzzy score, so the real user wins outright.
    read_norm = _norm(raw_name)
    exact = _exact_norm_hits(read_norm, registered, key=lambda p: p["username"])
    if len(exact) == 1:
        hit = exact[0]
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   hit["user_id"],
            "matched_username":  hit["username"],
            "confidence":        1.0,
            "matched_team_id":   hit.get("team_id"),
            "matched_team_name": hit.get("team_name"),
            "top_candidates": [{
                "user_id": hit["user_id"], "username": hit["username"],
                "team_name": hit.get("team_name"), "confidence": 1.0,
            }],
        }
    if len(exact) > 1:
        # MORE THAN ONE user normalizes to the same string ("KILLUA" vs "K1LLUA" - LEET_DIGITS folds
        # 1 -> i). There is no right answer to guess and a wrong pick silently credits another
        # player's kills, so we assert NOTHING and hand the reviewer the colliding users. Note this
        # cannot be left to fall through to the fuzzy pass: both would score 100 there and the row
        # would re-assert one of them at 1.0 on nothing better than alphabetical order.
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates": [
                {"user_id": p["user_id"], "username": p["username"],
                 "team_name": p.get("team_name"), "confidence": 1.0}
                for p in exact
            ],
        }

    # Step 3: fuzzy over the NORMALIZED pair - score the read AND a tag-stripped variant, keep each
    # username's best. (score_names owns the rapidfuzz import and returns 0.0 if it is unavailable,
    # so a missing rapidfuzz degrades to "no candidates" rather than raising.)
    #
    # Tag-stripped query variants. FF screenshots wrap the IGN in a short team tag, leading
    # ("SYN.ARDNT DS") or trailing ("NOXY CVS"), and which side is the tag is AMBIGUOUS: lead-
    # stripping "NOXY CVS" wrongly yields the bare tag "CVS", which substring-scores ~90 against
    # every same-tag TEAMMATE ("PUNKY CVS") - the owner's exact bug report. So we score the MAX
    # across the raw read plus every strip variant, but a stripped variant only qualifies when it
    # is >= 5 chars: tags are 1-5 chars, so a short remainder IS the tag, not the IGN. The raw
    # read alone scores same-tag teammates ~70, safely under the FE's 0.75 auto-pick gate, while
    # a genuine core ("AKAZA" vs "I AKAZA", "ARDNT" vs "ARENDT") boosts the true match high.
    _lead = re.compile(r"^[^\s.]{1,6}[.\s]+")
    _trail = re.compile(r"[.\s]+[^\s.]{1,6}$")
    variants = {raw_name}
    for v in (
        _lead.sub("", raw_name),
        _trail.sub("", raw_name),
        _trail.sub("", _lead.sub("", raw_name)),
        _lead.sub("", _trail.sub("", raw_name)),
    ):
        v = v.strip()
        if v and v.lower() != raw_name.lower() and len(v) >= 5:
            variants.add(v)
    # Normalize once per variant and once per username, then score through the shared ladder.
    #
    # PERFORMANCE: the bulk scan runs inside rapidfuzz's C loop (process.extract) over the
    # pre-normalized username list, not as a Python loop over the pool. match_name is called once per
    # read player and the standalone flow's pool is every user on the platform (~6.8k), so a Python
    # loop costs ~100 ms per read - seconds per map. We then apply the short-fragment cap to the
    # SHORTLIST process.extract returns. SHORTLIST_LIMIT is deliberately far larger than the 5
    # candidates we keep, so a capped entry losing its place cannot push the true match out of
    # contention; and the case that must never be truncated - normalized identity - is already
    # settled above by an exact comparison that does not go through this scan at all.
    query_norms = {q for q in (_norm(v) for v in variants) if q}
    norm_to_usernames = {}
    for p in registered:
        cand_norm = _norm(p["username"])
        if cand_norm:                      # a symbols-only username can never be identified
            norm_to_usernames.setdefault(cand_norm, []).append(p["username"])
    norm_keys = list(norm_to_usernames)

    best_scores = {}
    if _fuzz is not None and norm_keys:
        from rapidfuzz import process

        for q in query_norms:
            for cand_norm, raw_score, _idx in process.extract(
                q, norm_keys, scorer=_fuzz.WRatio,
                limit=SHORTLIST_LIMIT, score_cutoff=CANDIDATE_CUTOFF,
            ):
                # Re-score through the shared ladder so the short-fragment rules apply identically
                # to the team matcher (process.extract only gives us the raw WRatio).
                score = score_names(q, cand_norm)
                if score < CANDIDATE_CUTOFF:
                    continue
                for username in norm_to_usernames[cand_norm]:
                    if score > best_scores.get(username, 0):
                        best_scores[username] = score
    results = [
        # Deterministic order: score desc, then username, so a tie never shuffles between reads.
        (username, score, None)
        for username, score in sorted(best_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]

    if not results:
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates":    [],
        }

    top_candidates = []
    for username, score, _ in results:
        player = next((p for p in registered if p["username"] == username), None)
        if player:
            top_candidates.append({
                "user_id":    player["user_id"],
                "username":   username,
                # The candidate's CURRENT platform team (owner 2026-06-12) so the reviewer can
                # tell same-named players apart and sanity-check a match against the read team.
                "team_name":  player.get("team_name"),
                "confidence": round(score / 100, 3),
            })

    if not top_candidates:
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates":    [],
        }

    best = top_candidates[0]
    if best["confidence"] * 100 < MATCH_FLOOR:
        # Too weak to assert (see MATCH_FLOOR). Return the row UNMATCHED but keep every candidate, so
        # the review table shows "not on platform" with the near misses one click away rather than
        # pre-selecting a stranger who happened to score highest.
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates":    top_candidates,
        }
    player = next((p for p in registered if p["user_id"] == best["user_id"]), {})

    return {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_user_id":   best["user_id"],
        "matched_username":  best["username"],
        "confidence":        best["confidence"],
        "matched_team_id":   player.get("team_id"),
        "matched_team_name": player.get("team_name"),
        "top_candidates":    top_candidates,
    }


def detect_team_mismatches(draft_rows: list) -> list:
    """
    For each placement group, the players should all be on the same
    registered tournament team. If not, flag team_mismatch = True.

    Logic:
    - Group rows by placement number.
    - For each group, find the most common matched_team_id (majority vote).
    - Any player whose matched_team_id differs is flagged.
    """
    groups: dict = {}
    for row in draft_rows:
        p = row.get("placement", 0)
        groups.setdefault(p, []).append(row)

    result = []
    for placement, rows in groups.items():
        team_ids = [r.get("matched_team_id") for r in rows if r.get("matched_team_id")]

        if not team_ids:
            for row in rows:
                row["team_mismatch"]       = True
                row["admin_confirmed_sub"] = False
                row["expected_team_id"]    = None
            result.extend(rows)
            continue

        majority_team = Counter(team_ids).most_common(1)[0][0]

        for row in rows:
            row["expected_team_id"]    = majority_team
            row["team_mismatch"]       = (
                row.get("matched_team_id") is not None
                and row["matched_team_id"] != majority_team
            )
            row["admin_confirmed_sub"] = False
            result.append(row)

    return result
