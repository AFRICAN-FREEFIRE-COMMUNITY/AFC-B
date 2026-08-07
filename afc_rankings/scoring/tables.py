"""Admin-editable scoring tables - the values the engine reads, as data.

WHAT THIS IS
    ``constants.py`` holds the shipped defaults as Python literals. This module wraps
    exactly those values in a single frozen ``ScoringTables`` object, and adds the two
    conversions the admin surface needs:

        defaults_config()      ScoringTables -> the JSON blob an admin edits
        tables_from_config()   that JSON blob  -> ScoringTables the engine can read

    So the chain is:  constants.py  ->  ScoringTables  ->  engine functions,
    with an admin-saved ``ScoringConfig.config`` blob able to substitute for the middle
    step without a deploy.

WHY IT LIVES IN THE PURE PACKAGE
    ``scoring/`` is deliberately Django-free (see the package docstring and the
    ``TestNoDjangoImport`` probe in tests_scoring.py): no ORM, no I/O, same input ->
    same output. This module keeps that property - it converts dicts to dataclasses and
    nothing else. The DATABASE lookup that decides WHICH blob applies lives in the
    Django-aware layer, ``afc_rankings/aggregation.py`` (``resolve_tables`` /
    ``config_for_season``), which then passes the resulting ScoringTables down.

HOW IT CONNECTS
    * ``scoring/engine.py``          - every scoring function takes ``tables=DEFAULT_TABLES``.
    * ``afc_rankings/aggregation.py``- resolves the season's config row and builds tables.
    * ``afc_rankings/recalc.py``     - passes tables into assign_tier / player_tier and
                                       reads the participation floors from them.
    * ``afc_rankings/admin_scoring_config.py`` - serves ``defaults_config()`` to the admin
                                       editor and stores what comes back on ``ScoringConfig``.
    * ``scoring/validation.py``      - validates a blob BEFORE it is ever turned into tables.

CURRENCY (owner rule, and the cause of a real bug on 2026-08-03)
    Money thresholds in this system are authored in NAIRA, while an event's prize pool is
    stored in the event's own currency. A $400 event compared as the bare number 400
    against a 100,000 naira threshold matched nothing and fell to the bottom tier. So every
    money-denominated group is declared in ``FIELD_META`` with an explicit ``currency``,
    and the admin API serves that metadata alongside the values - the UI must never render
    a money threshold as a bare number.

RETIRE, NEVER DELETE
    A tier that past events were classified under cannot be removed from the table, or
    those events become unscoreable (``tier_multiplier("tier_2")`` would raise and every
    historical tier_2 result would break). A retired tier therefore STAYS in the table and
    stays resolvable for old results; ``retired`` only means "do not offer this for new
    work". ``ScoringTables.active_tier_keys`` is what pickers and validation should use;
    ``tier_multiplier`` resolves retired keys as well, on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from . import constants as C

# The blob schema version. Bumped when the SHAPE changes, not when values change.
# v1 = the original snapshot written by admin_scoring_config._defaults_snapshot (flat
# ``tier_multiplier`` / ``win_bonus`` dicts). v2 = the current shape, which merges those
# two dicts into one ordered ``tiers`` list so a tier can carry a label and a retired flag.
# ``tables_from_config`` reads BOTH, so v1 blobs already saved in production keep scoring.
#
# v3 adds ``tier_thresholds.mode`` and a ``count`` on each tier-cutoff row, for the owner's
# top-N tiering (constants.TIER_MODE_TOP_N). It is a pure ADDITION: a v2 blob with neither
# key reads back as threshold mode with no counts, which is exactly what it meant before.
SCHEMA_VERSION = 3

# Bracket tables are (upper_bound_inclusive | None, points) tuples in constants.py; in JSON
# they are [{"max": int|null, "points": number}] rows so there is no tuple/None ambiguity
# and the editor can render one row per band.
_BRACKET_GROUPS = (
    "kill_compression",
    "placement_compression",
    "prize_money_points",
    "social_media_points",
)


# ---------------------------------------------------------------------------
# Field metadata - what a number MEANS, so the UI cannot present it ambiguously
# ---------------------------------------------------------------------------
# Served by GET rankings/scoring-config/ as ``field_meta``. ``currency`` is null for every
# group that is not money; where it is set, the threshold values are in THAT currency and
# the UI must label them so. ``unit`` describes what the threshold counts, ``value_unit``
# what the resulting number is worth.
FIELD_META = MappingProxyType({
    "tiers": {
        "label": "Tournament tiers",
        "unit": None,
        "currency": None,
        "value_unit": "multiplier",
        "help": (
            "Each tier's scoring multiplier and flat win bonus. The multiplier applies to "
            "placement points, kill points and the finals bonus; the win bonus is flat. "
            "Retiring a tier hides it from new rules but keeps past events readable."
        ),
    },
    "placement_points": {
        "label": "Placement points per match",
        "unit": "finishing position",
        "currency": None,
        "value_unit": "points",
        "help": "Raw points for a single match finish. Any position not listed scores 0.",
    },
    "kill_compression": {
        "label": "Kill compression bands",
        "unit": "cumulative kills in one tournament",
        "currency": None,
        "value_unit": "points",
        "help": (
            "The band a team's kill total lands in decides the points, it is not additive. "
            "The last band must be open ended (no upper limit)."
        ),
    },
    "placement_compression": {
        "label": "Placement compression bands",
        "unit": "cumulative raw placement points in one tournament",
        "currency": None,
        "value_unit": "points",
        "help": (
            "The band a team's raw placement total lands in decides the points. "
            "The last band must be open ended (no upper limit)."
        ),
    },
    "finals_base": {
        "label": "Finals appearance base",
        "unit": None,
        "currency": None,
        "value_unit": "points",
        "help": "Multiplied by the tier multiplier for each finals appearance.",
    },
    "prize_money_points": {
        "label": "Prize money bands",
        # THE CURRENCY RULE. These thresholds are naira. An event's prize pool is stored in
        # the event's own currency and converted to naira before it reaches this table.
        "unit": "total prize money won in the quarter",
        "currency": "NGN",
        "value_unit": "points",
        "help": (
            "Thresholds are in Nigerian naira. Prize pools recorded in another currency are "
            "converted to naira at the stored exchange rate before they are compared here."
        ),
    },
    "social_media_points": {
        "label": "Social media bands",
        "unit": "combined Instagram + TikTok followers",
        "currency": None,
        "value_unit": "points",
        "help": "Quarterly only, and only for a snapshot an admin has verified.",
    },
    "tier_thresholds": {
        "label": "Ranking tiers",
        "unit": "quarterly score",
        "currency": None,
        "value_unit": "tier",
        "help": (
            "How a team's ranking tier is decided. In score mode a team reaches a tier by "
            "clearing its minimum quarterly score, so a tier can hold any number of teams. "
            "In top-N mode the tiers are fixed sizes: the best N teams on the season ladder "
            "are Tier 1, the next M are Tier 2, and so on. Either way a team below the "
            "participation floor gets the default tier and takes up no place."
        ),
        # The two ways the same table of tiers can be read. Served so the editor can render
        # the mode picker (and label the live column) without hardcoding these strings.
        "modes": [
            {
                "value": C.TIER_MODE_THRESHOLD,
                "label": "By score",
                "column": "min",
                "help": (
                    "A team is Tier 1 when it scores at least the Tier 1 cutoff. Tier sizes "
                    "vary with how the season goes: nobody has to reach Tier 1."
                ),
            },
            {
                "value": C.TIER_MODE_TOP_N,
                "label": "Top N on the ladder",
                "column": "count",
                "help": (
                    "Tier 1 is the top N teams on the final season ladder, whatever they "
                    "scored. Teams tied on the last place in a tier all get the higher tier, "
                    "so a tier can finish slightly larger than its count. Before the season "
                    "is evaluated this reads off the live ladder and is provisional."
                ),
            },
        ],
    },
    "scrim": {
        "label": "Scrim rules",
        "unit": None,
        "currency": None,
        "value_unit": "mixed",
        "help": (
            "Scrim placement and kills are worth 'weight' times their tournament value. The "
            "contribution is capped at the HIGHER of the flat allowance and the ratio of the "
            "team's tournament points, so competing more never costs a team scrim points."
        ),
    },
    "player_weights": {
        "label": "Player point weights",
        "unit": None,
        "currency": None,
        "value_unit": "points",
        "help": "Flat points a player earns per MVP, finals appearance, team win, and so on.",
    },
    "participation_floors": {
        "label": "Participation floors",
        "unit": "tournaments in the period",
        "currency": None,
        "value_unit": "count",
        "help": (
            "The minimum number of tournaments needed to appear on a ladder (monthly) or to "
            "be tiered on score rather than dropped to the default tier (quarterly)."
        ),
    },
    # Not part of the scoring blob, but the same currency trap applies, so the admin API
    # publishes it here too: an EventTierRule condition on `prize` is compared in naira.
    "event_tier_rule_prize": {
        "label": "Tournament tier rule prize threshold",
        "unit": "event prize pool",
        "currency": "NGN",
        "value_unit": "tier",
        "help": (
            "Tier rule prize thresholds are COMPARED in Nigerian naira. An event whose pool is "
            "recorded in another currency is converted to naira before the rule is tested, and "
            "since 2026-08-07 a threshold may itself be written in another currency, in which "
            "case it is converted the same way at the rate in force when the event is classified. "
            "A threshold with no currency of its own means naira."
        ),
    },
})

# Convenience for callers that only want "which of these are money, and in what".
CURRENCY_BY_FIELD = MappingProxyType(
    {key: meta["currency"] for key, meta in FIELD_META.items()}
)


# ---------------------------------------------------------------------------
# The tables object the engine reads
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TierDef:
    """One tournament tier: its multiplier, its flat win bonus, and whether it is retired.

    ``key`` is the string stored on ``Event.tournament_tier`` ("tier_1"...), so it is a
    permanent identifier and is never reused. ``label`` is free text the admin can rename
    without touching any event row. ``retired`` keeps the tier resolvable for events already
    classified under it while removing it from the pickers used for new work.
    """

    key: str
    label: str
    multiplier: float
    win_bonus: float
    retired: bool = False


@dataclass(frozen=True)
class ScoringTables:
    """Every number the scoring engine uses, in one immutable object.

    Built either from ``constants.py`` (``DEFAULT_TABLES``) or from an admin-saved JSON blob
    (``tables_from_config``). Passed into every engine function as ``tables=``; the engine
    never reaches for a module-level constant when a table is supplied, which is what makes
    a config change take effect without a deploy and keeps historical seasons on their own
    rules.
    """

    tiers: tuple[TierDef, ...]
    placement_points: dict[int, float]
    kill_compression: tuple[tuple[int | None, float], ...]
    placement_compression: tuple[tuple[int | None, float], ...]
    finals_base: float
    prize_money_points: tuple[tuple[int | None, float], ...]
    social_media_points: tuple[tuple[int | None, float], ...]
    tier_thresholds: tuple[tuple[float, int], ...]
    tier_default: int
    tier_rank_labels: dict[int, str]
    # HOW the tiers above are decided: constants.TIER_MODE_THRESHOLD (a team clears a score)
    # or TIER_MODE_TOP_N (a team is one of the best N on the ladder). Threshold is the
    # default, so a config that never mentions the mode behaves exactly as it always did.
    # ``tier_counts`` is the top-N column of the SAME tier rows, highest tier first, as
    # (count | None, tier_int). None means the admin has not set a size for that tier; in
    # top-N mode validation refuses to save that, and the engine treats it as an empty tier.
    tier_mode: str
    tier_counts: tuple[tuple[int | None, int], ...]
    scrim_weight: float
    scrim_win_flat: float
    scrim_cap_ratio: float
    scrim_flat_cap: float
    scrim_daily_cap: int
    scrim_monthly_cap: int
    player_mvp_pts: float
    player_finals_pts: float
    player_team_win_pts: float
    player_participation_pts: float
    player_scrim_win_pts: float
    player_scrim_kill_weight: float
    team_monthly_floor: int
    team_quarterly_floor: int
    player_monthly_floor: int
    player_quarterly_floor: int
    # Set by tables_from_config when the blob came from a saved version, so a caller can say
    # which rules produced a score. None means "the shipped defaults".
    source_version: int | None = field(default=None)

    # ── tier lookups ──
    def tier(self, key: str) -> TierDef:
        """Resolve a tier by its stored key, RETIRED ONES INCLUDED.

        Deliberate: an event classified as tier_2 years ago must still score after tier_2 is
        retired. Retirement is a rule about new work, not a deletion. Raises ValueError only
        when the key was never defined at all.
        """
        for t in self.tiers:
            if t.key == key:
                return t
        raise ValueError(f"unknown tournament tier: {key!r}")

    @property
    def active_tier_keys(self) -> tuple[str, ...]:
        """Tier keys an admin may pick for NEW rules (retired ones excluded)."""
        return tuple(t.key for t in self.tiers if not t.retired)

    @property
    def tier_multiplier_map(self) -> dict[str, float]:
        return {t.key: t.multiplier for t in self.tiers}

    @property
    def win_bonus_map(self) -> dict[str, float]:
        return {t.key: t.win_bonus for t in self.tiers}


# ---------------------------------------------------------------------------
# Defaults, straight from constants.py
# ---------------------------------------------------------------------------
def _default_tiers() -> tuple[TierDef, ...]:
    """The shipped tiers. Labels are the human names; keys match Event.tournament_tier."""
    labels = {"tier_1": "Tier 1", "tier_2": "Tier 2", "tier_3": "Tier 3"}
    return tuple(
        TierDef(
            key=key,
            label=labels.get(key, key),
            multiplier=float(mult),
            win_bonus=float(C.WIN_BONUS.get(key, 0)),
            retired=False,
        )
        for key, mult in C.TIER_MULTIPLIER.items()
    )


DEFAULT_TABLES = ScoringTables(
    tiers=_default_tiers(),
    placement_points={int(k): float(v) for k, v in C.PLACEMENT_POINTS.items()},
    kill_compression=tuple(C.KILL_COMPRESSION),
    placement_compression=tuple(C.PLACEMENT_COMPRESSION),
    finals_base=float(C.FINALS_BASE),
    prize_money_points=tuple(C.PRIZE_MONEY_POINTS),
    social_media_points=tuple(C.SOCIAL_MEDIA_POINTS),
    tier_thresholds=tuple((float(m), int(t)) for m, t in C.TIER_THRESHOLDS),
    tier_default=int(C.TIER_DEFAULT),
    tier_rank_labels={int(k): v for k, v in C.TIER_LABELS.items()},
    # Ships in threshold mode with NO top-N sizes. Deliberately not seeded with invented
    # numbers: how many teams belong in Tier 1 is a judgement about the size of the scene,
    # so an admin who switches to top-N states it and validation refuses a blank.
    tier_mode=C.TIER_MODE_DEFAULT,
    tier_counts=tuple((None, int(t)) for _, t in C.TIER_THRESHOLDS),
    scrim_weight=float(C.SCRIM_WEIGHT),
    scrim_win_flat=float(C.SCRIM_WIN_FLAT),
    scrim_cap_ratio=float(C.SCRIM_CAP_RATIO),
    scrim_flat_cap=float(C.SCRIM_FLAT_CAP),
    scrim_daily_cap=int(C.SCRIM_DAILY_CAP),
    scrim_monthly_cap=int(C.SCRIM_MONTHLY_CAP),
    player_mvp_pts=float(C.PLAYER_MVP_PTS),
    player_finals_pts=float(C.PLAYER_FINALS_PTS),
    player_team_win_pts=float(C.PLAYER_TEAM_WIN_PTS),
    player_participation_pts=float(C.PLAYER_PARTICIPATION_PTS),
    player_scrim_win_pts=float(C.PLAYER_SCRIM_WIN_PTS),
    player_scrim_kill_weight=float(C.PLAYER_SCRIM_KILL_WEIGHT),
    # Participation floors were hardcoded in recalc.py (§5.2 monthly >= 1 tournament,
    # §7.4 team quarterly >= 2, §9.2 player quarterly >= 1). They live here now so an
    # admin can move them; the numbers are unchanged from what shipped.
    team_monthly_floor=1,
    team_quarterly_floor=2,
    player_monthly_floor=1,
    player_quarterly_floor=1,
)


# ---------------------------------------------------------------------------
# ScoringTables -> JSON blob (what the admin editor receives)
# ---------------------------------------------------------------------------
def _brackets_to_json(table) -> list[dict]:
    return [{"max": upper, "points": points} for (upper, points) in table]


def _count_for_tier(t: ScoringTables, tier: int):
    """The top-N size configured for a tier int, or None when it has none."""
    for count, tier_int in t.tier_counts:
        if tier_int == tier:
            return count
    return None


def config_from_tables(t: ScoringTables) -> dict:
    """Serialise a ScoringTables into the editable JSON blob.

    Every key here is round-trippable through ``tables_from_config``: what the editor
    receives is exactly what it may send back.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "tiers": [
            {
                "key": d.key,
                "label": d.label,
                "multiplier": d.multiplier,
                "win_bonus": d.win_bonus,
                "retired": d.retired,
            }
            for d in t.tiers
        ],
        # JSON object keys must be strings; tables_from_config coerces them back to int.
        "placement_points": {str(k): v for k, v in sorted(t.placement_points.items())},
        "kill_compression": _brackets_to_json(t.kill_compression),
        "placement_compression": _brackets_to_json(t.placement_compression),
        "finals_base": t.finals_base,
        "prize_money_points": _brackets_to_json(t.prize_money_points),
        "social_media_points": _brackets_to_json(t.social_media_points),
        # One ordered list of tiers, highest first, carrying BOTH columns: ``min`` is what
        # threshold mode reads and ``count`` is what top-N mode reads. Both are always
        # emitted so flipping the mode back and forth never loses the other set of numbers.
        # ``counts_by_tier`` is keyed on the tier int rather than on list position, so a
        # count follows its tier even if the rows are reordered.
        "tier_thresholds": {
            "mode": t.tier_mode,
            "brackets": [
                {"min": m, "tier": tier, "count": _count_for_tier(t, tier)}
                for (m, tier) in t.tier_thresholds
            ],
            "default_tier": t.tier_default,
            "labels": {str(k): v for k, v in sorted(t.tier_rank_labels.items())},
        },
        "scrim": {
            "weight": t.scrim_weight,
            "win_flat": t.scrim_win_flat,
            "cap_ratio": t.scrim_cap_ratio,
            "flat_cap": t.scrim_flat_cap,
            "daily_cap": t.scrim_daily_cap,
            "monthly_cap": t.scrim_monthly_cap,
        },
        "player_weights": {
            "mvp_pts": t.player_mvp_pts,
            "finals_pts": t.player_finals_pts,
            "team_win_pts": t.player_team_win_pts,
            "participation_pts": t.player_participation_pts,
            "scrim_win_pts": t.player_scrim_win_pts,
            "scrim_kill_weight": t.player_scrim_kill_weight,
        },
        "participation_floors": {
            "team_monthly": t.team_monthly_floor,
            "team_quarterly": t.team_quarterly_floor,
            "player_monthly": t.player_monthly_floor,
            "player_quarterly": t.player_quarterly_floor,
        },
    }


def defaults_config() -> dict:
    """The factory-reset blob: constants.py rendered in the editable JSON shape."""
    return config_from_tables(DEFAULT_TABLES)


# Every top-level key a saved blob may carry. Anything else is rejected on save, because a
# mistyped key would silently do nothing while the admin believed the change had landed.
# The two legacy names are accepted so v1 blobs written before this build still load.
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    set(defaults_config().keys())
    | {
        "schema_version",
        # v1 legacy shapes, read by tables_from_config, never emitted:
        "tier_multiplier",
        "win_bonus",
        "scrim_flat_cap",
    }
)


# ---------------------------------------------------------------------------
# JSON blob -> ScoringTables (what the engine reads)
# ---------------------------------------------------------------------------
def _num(value, fallback):
    """Coerce to float, falling back rather than raising - a scoring run must never die
    on configuration. Validation (validation.py) is what refuses bad values at SAVE time;
    by the time a blob reaches here it has already been accepted, and anything still odd
    (a hand-edited row in the database, say) degrades to the shipped default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _brackets_from_json(rows, fallback):
    """[{"max": int|null, "points": n}] -> ((upper|None, points), ...), order preserved.

    Order is preserved rather than sorted: the engine's lookup is first-match-wins down the
    list, so re-sorting here would silently change which band an admin's table selects.
    Ordering problems are reported by validation.py as contradictions instead.
    """
    if not isinstance(rows, list) or not rows:
        return fallback
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return fallback
        upper = row.get("max")
        upper = None if upper is None else _int(upper, None)
        points = _num(row.get("points"), None)
        if points is None:
            return fallback
        out.append((upper, points))
    return tuple(out)


def _tiers_from_json(blob) -> tuple[TierDef, ...]:
    """Read the v2 ``tiers`` list, or fall back to the v1 ``tier_multiplier`` +
    ``win_bonus`` dict pair, or to the shipped defaults."""
    rows = blob.get("tiers")
    if isinstance(rows, list) and rows:
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            key = str(row["key"])
            out.append(TierDef(
                key=key,
                label=str(row.get("label") or key),
                multiplier=_num(row.get("multiplier"), 1.0),
                win_bonus=_num(row.get("win_bonus"), 0.0),
                retired=bool(row.get("retired", False)),
            ))
        if out:
            return tuple(out)

    # v1 fallback: two flat dicts keyed by tier string.
    mults = blob.get("tier_multiplier")
    if isinstance(mults, dict) and mults:
        bonuses = blob.get("win_bonus") if isinstance(blob.get("win_bonus"), dict) else {}
        return tuple(
            TierDef(
                key=str(key),
                label=str(key).replace("_", " ").title(),
                multiplier=_num(mult, 1.0),
                win_bonus=_num(bonuses.get(key), 0.0),
                retired=False,
            )
            for key, mult in mults.items()
        )
    return DEFAULT_TABLES.tiers


def tables_from_config(blob, *, version: int | None = None) -> ScoringTables:
    """Build ScoringTables from a saved ``ScoringConfig.config`` blob.

    Missing or unreadable keys fall back to the shipped default for that key ONLY, so a
    partial blob (for example the one-key ``{"scrim_flat_cap": 8}`` written before this
    build) is a valid config that overrides just that value. Never raises: a scoring run
    must not fail because of configuration. ``version`` is stamped onto the result so a
    caller can report which rules produced a score.
    """
    d = DEFAULT_TABLES
    if not isinstance(blob, dict):
        return d

    thresholds_blob = blob.get("tier_thresholds")
    if isinstance(thresholds_blob, dict):
        rows = thresholds_blob.get("brackets")
        if isinstance(rows, list) and rows:
            parsed, counts = [], []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                min_score = _num(row.get("min"), None)
                tier_int = _int(row.get("tier"), None)
                if min_score is None or tier_int is None:
                    continue
                parsed.append((min_score, tier_int))
                # A row with no ``count`` (every pre-v3 blob) keeps None: unset, not zero.
                counts.append((_int(row.get("count"), None), tier_int))
            if parsed:
                thresholds, tier_counts = tuple(parsed), tuple(counts)
            else:
                thresholds, tier_counts = d.tier_thresholds, d.tier_counts
        else:
            thresholds, tier_counts = d.tier_thresholds, d.tier_counts
        # An unrecognised mode falls back to the shipped default rather than raising: a
        # scoring run must never die on configuration (validation refuses it at save time).
        mode = thresholds_blob.get("mode")
        tier_mode = mode if mode in C.TIER_MODES else d.tier_mode
        tier_default = _int(thresholds_blob.get("default_tier"), d.tier_default)
        labels_blob = thresholds_blob.get("labels")
        if isinstance(labels_blob, dict) and labels_blob:
            rank_labels = {}
            for key, label in labels_blob.items():
                as_int = _int(key, None)
                if as_int is not None:
                    rank_labels[as_int] = str(label)
            rank_labels = rank_labels or dict(d.tier_rank_labels)
        else:
            rank_labels = dict(d.tier_rank_labels)
    else:
        thresholds, tier_default = d.tier_thresholds, d.tier_default
        tier_counts, tier_mode = d.tier_counts, d.tier_mode
        rank_labels = dict(d.tier_rank_labels)

    placement_blob = blob.get("placement_points")
    if isinstance(placement_blob, dict) and placement_blob:
        placement = {}
        for key, value in placement_blob.items():
            finish = _int(key, None)
            points = _num(value, None)
            if finish is not None and points is not None:
                placement[finish] = points
        placement = placement or dict(d.placement_points)
    else:
        placement = dict(d.placement_points)

    scrim = blob.get("scrim") if isinstance(blob.get("scrim"), dict) else {}
    # ``scrim_flat_cap`` at the top level is the pre-v2 spelling that aggregation
    # .scrim_flat_cap() already reads; honour it so nothing saved before this build changes
    # meaning. The nested ``scrim.flat_cap`` wins when both are present.
    flat_cap = scrim.get("flat_cap", blob.get("scrim_flat_cap"))

    weights = blob.get("player_weights") if isinstance(blob.get("player_weights"), dict) else {}
    floors = (blob.get("participation_floors")
              if isinstance(blob.get("participation_floors"), dict) else {})

    return ScoringTables(
        tiers=_tiers_from_json(blob),
        placement_points=placement,
        kill_compression=_brackets_from_json(blob.get("kill_compression"), d.kill_compression),
        placement_compression=_brackets_from_json(
            blob.get("placement_compression"), d.placement_compression),
        finals_base=_num(blob.get("finals_base"), d.finals_base),
        prize_money_points=_brackets_from_json(
            blob.get("prize_money_points"), d.prize_money_points),
        social_media_points=_brackets_from_json(
            blob.get("social_media_points"), d.social_media_points),
        tier_thresholds=thresholds,
        tier_default=tier_default,
        tier_rank_labels=rank_labels,
        tier_mode=tier_mode,
        tier_counts=tier_counts,
        scrim_weight=_num(scrim.get("weight"), d.scrim_weight),
        scrim_win_flat=_num(scrim.get("win_flat"), d.scrim_win_flat),
        scrim_cap_ratio=_num(scrim.get("cap_ratio"), d.scrim_cap_ratio),
        scrim_flat_cap=_num(flat_cap, d.scrim_flat_cap),
        scrim_daily_cap=_int(scrim.get("daily_cap"), d.scrim_daily_cap),
        scrim_monthly_cap=_int(scrim.get("monthly_cap"), d.scrim_monthly_cap),
        player_mvp_pts=_num(weights.get("mvp_pts"), d.player_mvp_pts),
        player_finals_pts=_num(weights.get("finals_pts"), d.player_finals_pts),
        player_team_win_pts=_num(weights.get("team_win_pts"), d.player_team_win_pts),
        player_participation_pts=_num(
            weights.get("participation_pts"), d.player_participation_pts),
        player_scrim_win_pts=_num(weights.get("scrim_win_pts"), d.player_scrim_win_pts),
        player_scrim_kill_weight=_num(
            weights.get("scrim_kill_weight"), d.player_scrim_kill_weight),
        team_monthly_floor=_int(floors.get("team_monthly"), d.team_monthly_floor),
        team_quarterly_floor=_int(floors.get("team_quarterly"), d.team_quarterly_floor),
        player_monthly_floor=_int(floors.get("player_monthly"), d.player_monthly_floor),
        player_quarterly_floor=_int(floors.get("player_quarterly"), d.player_quarterly_floor),
        source_version=version,
    )


# ---------------------------------------------------------------------------
# Reachability - used by validation.py and worth having on its own
# ---------------------------------------------------------------------------
def max_achievable_score(t: ScoringTables) -> float:
    """The highest score a team could reach from ONE perfect tournament under these tables.

    Top compression band on both scales, the best tier multiplier, the win bonus, a finals
    appearance, the full scrim allowance, and the top prize and social bands. Deliberately
    computed for a single tournament: a team that plays several can exceed it, so this is a
    conservative lower bound on what is reachable. Validation uses it for exactly one
    judgement - if even this cannot reach the LOWEST tier cutoff, no team can ever leave the
    default tier and the scale is unusable.
    """
    live = [d for d in t.tiers if not d.retired] or list(t.tiers)
    best_mult = max((d.multiplier for d in live), default=1.0)
    best_win = max((d.win_bonus for d in live), default=0.0)
    top_kill = max((points for _, points in t.kill_compression), default=0.0)
    top_place = max((points for _, points in t.placement_compression), default=0.0)
    top_prize = max((points for _, points in t.prize_money_points), default=0.0)
    top_social = max((points for _, points in t.social_media_points), default=0.0)
    per_tournament = (top_place + top_kill) * best_mult + best_win + t.finals_base * best_mult
    return per_tournament + t.scrim_flat_cap + top_prize + top_social
