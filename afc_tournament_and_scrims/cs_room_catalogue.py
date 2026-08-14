"""
Clash Squad ROOM SETTINGS catalogue (owner 2026-08-12).

WHAT THIS IS: the single place that knows what a Free Fire custom Clash Squad room can be set to -
every dropdown's options, every yes/no toggle, the store list with Garena's default prices, the
per-map areas, and the built-in preset modes. Nothing else in the codebase hardcodes these.

WHY A CATALOGUE MODULE AND NOT COLUMNS: Garena changes this list every patch - a new gun, a new
preset, a renamed area. Column-per-setting would mean a migration each time and a frontend that
drifts from the backend. Here the option lists are data, the config models store the chosen values,
and the FE fetches the catalogue instead of duplicating it (GET events/cs-room-catalogue/).

SOURCE: the owner's 36 screenshots of the in-game room screen, 2026-08-12. Where a dropdown's full
list was not visible in a screenshot the values captured are marked below, so a later reader knows
the difference between "Free Fire only offers these" and "this is what we could see".

CONSUMED BY: cs_room_views.py (serves the catalogue + validates a submitted config),
models.CSRoomConfig / CSRoomPreset (defaults), and the FE room-settings editor.
"""

# ── core dropdowns ───────────────────────────────────────────────────────────────────────────
# Rounds: the main picker showed 7/13/11/5 and the economy tab's picker showed 9/11/13/15, so the
# real set is every odd number from 5 to 15. Kept sorted for display.
ROUND_CHOICES = [5, 7, 9, 11, 13, 15]

# Starting economy tier. "esports" is Free Fire's own named tier, not a number.
ECONOMY_CHOICES = [
    ("500", "500"),
    ("1500", "1500"),
    ("9950", "9950"),
    ("esports", "Esports Mode"),
]

SPECIAL_MODE_CHOICES = [
    ("no", "No"),
    ("duo_active_skills", "Duo Active Skills"),
    ("solara", "Solara"),
    ("bermuda", "Bermuda"),
    ("kalahari", "Kalahari"),
    ("nexterra", "Nexterra"),
]

SPECIAL_AIRDROP_CHOICES = [
    ("no", "No"),
    ("cyber_airdrop", "Cyber Airdrop"),
]

HP_CHOICES = [200, 500, 50, 1]          # order as the game lists them
EP_CHOICES = [0, 50, 100, 200]          # only 0 was visible in the screenshots; the rest are the
                                        # values Free Fire offers today. Widen here if a room shows more.
MOVEMENT_SPEED_CHOICES = [100, 150, 200]   # percentages; only 100% was visible
JUMP_HEIGHT_CHOICES = [100, 150, 200]      # percentages; only 100% was visible

ENVIRONMENT_CHOICES = [("day", "Day"), ("night", "Night")]

# Maps a Clash Squad room can be played on.
MAP_CHOICES = [
    ("nexterra", "Nexterra"),
    ("purgatory", "Purgatory"),
    ("kalahari", "Kalahari"),
    ("solara", "Solara"),
    ("bermuda", "Bermuda"),
]

# ── toggles ──────────────────────────────────────────────────────────────────────────────────
# key -> (label, default). All are yes/no except environment, which has its own choice list above.
# Defaults are what a fresh Free Fire room opens with, per the screenshots (the highlighted side).
TOGGLES = {
    "ammo_limit":          ("Ammo limit", True),
    "throwable_limit":     ("Throwable limit", True),
    "airdrop":             ("Airdrop", True),
    "high_tier_loot_zone": ("High tier loot zone", True),
    "supply_gadget":       ("Supply gadget", False),
    "event_gameplay":      ("Event gameplay", False),
    "generic_enemy_outfit": ("Generic enemy outfit", False),
    "friendly_fire":       ("Friendly fire", False),
    "precise_aim":         ("Precise aim", True),
    "character_skill":     ("Character skill", True),
    "loadout":             ("Loadout", True),
    "gun_attributes":      ("Gun attributes", True),
    "headshot":            ("Headshot", False),
    "one_active_limit":    ("One-active limit", False),
    "skill_ban_and_pick":  ("Skill ban and pick", False),
    "death_spectate":      ("Death spectate", True),
    "stats":               ("Stats", True),
    "hide_nickname":       ("Hide nickname", True),
    "save_replays":        ("Save replays", True),
    "block_emulators":     ("Block emulators", True),
    "players_access":      ("Player's access", False),
    "display_scores":      ("Display scores", False),
    "backpack_logo":       ("Backpack logo", False),
    "replay_final_shot":   ("Replay final shot", False),
}

# ── store ────────────────────────────────────────────────────────────────────────────────────
# (code, label, default price). Prices are Free Fire's defaults as read off the screenshots; an
# organizer can change any of them per room, which is the point of the STORE tab. Order follows the
# in-game list so an organizer comparing the two screens sees the same sequence.
STORE_WEAPONS = [
    ("scythe", "Scythe", 500), ("katana", "Katana", 500),
    ("m500", "M500", 400), ("g18", "G18", 500),
    ("usp2", "USP-2", 500), ("desert_eagle", "Desert Eagle", 800),
    ("m1873", "M1873", 500), ("mini_uzi", "Mini Uzi", 800),
    ("m1917", "M1917", 800), ("an94", "AN94", 1000),
    ("g36", "G36", 1000), ("kingfisher", "Kingfisher", 1000),
    ("m60", "M60", 1000),
    ("m60_i", "Upgrade M60 to M60-I", 200),
    ("m60_ii", "Upgrade M60-I to M60-II", 400),
    ("m60_iii", "Upgrade M60-II to M60-III", 900),
    ("m4a1", "M4A1", 1000),
    ("m4a1_i", "Upgrade M4A1 to M4A1-I", 200),
    ("m4a1_ii", "Upgrade M4A1-I to M4A1-II", 400),
    ("m4a1_iii", "Upgrade M4A1-II to M4A1-III", 900),
    ("mp5", "MP5", 1300),
    ("mp5_i", "Upgrade MP5 to MP5-I", 200),
    ("mp5_ii", "Upgrade MP5-I to MP5-II", 400),
    ("mp5_iii", "Upgrade MP5-II to MP5-III", 900),
    ("mac10", "MAC10", 1300),
    ("mac10_i", "Upgrade MAC10 to MAC10-I", 200),
    ("mac10_ii", "Upgrade MAC10-I to MAC10-II", 400),
    ("mac10_iii", "Upgrade MAC10-II to MAC10-III", 900),
    ("spas12", "SPAS12", 1300), ("scar", "SCAR", 1400),
    ("scar_i", "Upgrade SCAR to SCAR-I", 200),
    ("scar_ii", "Upgrade SCAR-I to SCAR-II", 400),
    ("scar_iii", "Upgrade SCAR-II to SCAR-III", 900),
    ("vector", "Vector", 1400), ("bizon", "Bizon", 1400),
    ("aug", "AUG", 1400),
    ("aug_i", "Upgrade AUG to AUG-I", 200),
    ("aug_ii", "Upgrade AUG-I to AUG-II", 400),
    ("aug_iii", "Upgrade AUG-II to AUG-III", 900),
    ("vss", "VSS", 1500), ("xm8", "XM8", 1500),
    ("ak47", "AK47", 1500), ("m14", "M14", 1500),
    ("m14_i", "Upgrade M14 to M14-I", 200),
    ("m14_ii", "Upgrade M14-I to M14-II", 400),
    ("m14_iii", "Upgrade M14-II to M14-III", 900),
    ("ump", "UMP", 1500), ("p90", "P90", 1500),
    ("vsk94", "VSK94", 1500), ("famas", "FAMAS", 1600),
    ("m1014", "M1014", 1600),
    ("m1014_i", "Upgrade M1014 to M1014-I", 200),
    ("m1014_ii", "Upgrade M1014-I to M1014-II", 400),
    ("m1014_iii", "Upgrade M1014-II to M1014-III", 900),
    ("parafal", "Parafal", 1600), ("mag7", "MAG-7", 1600),
    ("m590", "M590", 1600), ("charge_buster", "Charge Buster", 1600),
    ("winchester_ii", "Winchester-II", 1700),
    ("winchester_iii", "Upgrade Winchester-II to Winchester-III", 600),
    ("kord", "Kord", 1700), ("trogon", "Trogon", 1800),
    ("thompson", "Thompson", 1800), ("sks", "SKS", 1800),
    ("kar98k", "Kar98k", 1800), ("m24", "M24", 1800),
    ("m24_i", "Upgrade M24 to M24-I", 200),
    ("m24_ii", "Upgrade M24-I to M24-II", 400),
    ("m24_iii", "Upgrade M24-II to M24-III", 900),
    ("m1887", "M1887", 1800), ("mp40", "MP40", 1900),
    ("groza", "Groza", 1900), ("ac80", "AC80", 1900),
    ("woodpecker", "Woodpecker", 1900), ("m14_i_weapon", "M14-I", 1900),
    ("m249", "M249", 2000), ("svd", "SVD", 2200),
    ("m82b", "M82B", 2200), ("awm", "AWM", 2200),
    ("heal_sniper", "Heal Sniper", 2200), ("awm_y", "AWM-Y", 2200),
    ("cg15", "CG15", 2200), ("groza_x", "Groza-X", 2600),
    ("svd_y", "SVD-Y", 2600), ("m1887_x", "M1887-X", 2600),
    ("thompson_x", "Thompson-X", 2600), ("m249_x", "M249-X", 2800),
    ("m79", "M79", 3000), ("gatling", "Gatling", 3000),
    ("flamethrower", "Flamethrower", 100),
    ("thermal_scope", "Thermal Scope", 100),
    ("grenade", "Grenade", 300), ("flash_freeze", "Flash Freeze", 300),
    ("flashbang", "Flashbang", 200), ("smoke_grenade", "Smoke Grenade", 100),
    ("dragon_freeze", "Dragon Freeze", 200), ("gloo_wall", "Gloo Wall", 300),
]

STORE_ITEMS = [
    ("vest_2", "Vest Lv. 2", 400), ("vest_3", "Vest Lv. 3", 1000),
    ("vest_4", "Vest Lv. 4", 1000), ("helmet_2", "Helmet Lv. 2", 200),
    ("helmet_3", "Helmet Lv. 3", 400), ("helmet_4", "Helmet Lv. 4", 800),
    ("repair_kit", "Repair Kit", 200), ("horizaline", "Horizaline", 1200),
    ("vest_2_to_3", "Upgrade Vest Lv. 2 to Lv. 3", 600),
    ("vest_3_to_4", "Upgrade Vest Lv. 3 to Lv. 4", 500),
    ("helmet_2_to_3", "Upgrade Helmet Lv. 2 to Lv. 3", 500),
    ("helmet_3_to_4", "Upgrade Helmet Lv. 3 to Lv. 4", 400),
    ("dragon_sprinters", "Dragon Sprinters", 200),
    ("magnetic_shield", "Magnetic Shield", 300),
    ("jumping_shoes", "Jumping Shoes", 200), ("hook_gun", "Hook Gun", 200),
    ("brick_grenade", "Brick Grenade", 1000), ("landmine", "Landmine", 1000),
    ("jet_pack_shoes", "Jet Pack Shoes", 1000),
    ("decoy_grenade", "Decoy Grenade", 300),
    ("mini_turret", "Mini Turret", 300), ("uav_lite", "UAV-Lite", 1000),
    ("mushroom_3", "Mushroom Lv. 3", 100), ("restore_ep", "Restore EP", 300),
    ("quick_ep_conversion", "Quick EP Conversion", 500),
    ("speed_boost", "Speed Boost", 1500), ("med_kit_perk", "Med Kit Perk", 200),
]

# ── economy ──────────────────────────────────────────────────────────────────────────────────
# Free Fire's default starting cash per round, and the bonuses paid for events during a round.
DEFAULT_ROUND_ECONOMY = {1: 500, 2: 900, 3: 1100, 4: 1700, 5: 2100, 6: 2400, 7: 3000}
ECONOMY_EVENTS = {
    "winning_round":        ("Winning round", 500),
    "elimination":          ("Elimination", 200),
    "losing_round":         ("Losing round", 200),
    "losing_streak_2":      ("2-rounds losing streak", 1000),
    "losing_streak_3":      ("3-rounds losing streak", 2400),
    "first_blood":          ("First blood", 100),
}

# ── areas per map ────────────────────────────────────────────────────────────────────────────
# Which part of the map a given round is played in. Codes are stable; labels are what the game shows.
MAP_AREAS = {
    "nexterra": [
        ("deca_square", "Deca Square"), ("grav_labs", "Grav Labs"),
        ("farmtopia", "Farmtopia"), ("museum", "Museum"),
        ("intellect_center", "Intellect Center"), ("mud_site", "Mud Site"),
        ("zipway", "Zipway"), ("rust_town", "Rust Town"),
    ],
    "purgatory": [
        ("central", "Central"), ("brasilia", "Brasilia"), ("forge", "Forge"),
        ("lumber_mill", "Lumber Mill"), ("crossroads", "Crossroads"),
        ("marbleworks", "Marbleworks"),
    ],
    "kalahari": [
        ("command_post", "Command Post"), ("confinement", "Confinement"),
        ("shrines", "Shrines"), ("the_maze", "The Maze"), ("mammoth", "Mammoth"),
        ("council_hall", "Council Hall"), ("bayfront", "Bayfront"),
        ("santa_catarina", "Santa Catarina"), ("refinery", "Refinery"),
    ],
    "solara": [
        ("windmill", "Windmill"), ("aquarium", "Aquarium"), ("eco_drain", "Eco Drain"),
        ("bayside", "Bayside"), ("bloomtown", "Bloomtown"), ("studio", "Studio"),
        ("the_hub", "The Hub"), ("archway", "Archway"),
    ],
    # Bermuda appears as a Special mode rather than a Clash Squad map in the screenshots supplied,
    # so it has no area list yet. Left empty rather than invented.
    "bermuda": [],
}

# ── preset modes ─────────────────────────────────────────────────────────────────────────────
# The six one-tap modes Free Fire offers. Each is a PARTIAL config: applying one overwrites the keys
# it names and leaves everything else as it was, which is how the in-game buttons behave.
#
# Only "esports" is pinned down by the screenshots (Economy shows an "Esports Mode" tier). The rest
# are described by what the mode is known to do; an organizer can adjust anything after applying,
# and the values below are the starting point rather than a claim about Garena's exact internals.
PRESET_MODES = {
    "random_store": {
        "label": "Random Store",
        "description": "Everyone buys from the same randomised shortlist each round.",
        "config": {"economy": "500", "rounds": 7},
    },
    "competitive_store": {
        "label": "Competitive Store",
        "description": "The tuned store used for serious matches.",
        "config": {"economy": "500", "rounds": 7, "toggles": {"headshot": False}},
    },
    "crazy_store": {
        "label": "Crazy Store",
        "description": "Everything cheap and available. Chaotic, good for showmatches.",
        "config": {"economy": "9950", "rounds": 7},
    },
    "hardcore_mode": {
        "label": "Hardcore Mode",
        "description": "Low health, no second chances.",
        "config": {"hp": 50, "rounds": 7, "toggles": {"headshot": True, "precise_aim": True}},
    },
    "cs_elite": {
        "label": "CS Elite",
        "description": "The ranked Clash Squad ruleset.",
        "config": {"economy": "1500", "rounds": 7},
    },
    "esports_mode": {
        "label": "Esports Mode",
        "description": "Tournament defaults: 13 rounds and the esports economy.",
        "config": {
            "economy": "esports",
            "rounds": 13,
            "toggles": {
                "block_emulators": True, "save_replays": True, "stats": True,
                "friendly_fire": False, "hide_nickname": False,
            },
        },
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────────────────────
def default_toggles():
    """A fresh room's yes/no settings."""
    return {key: default for key, (_label, default) in TOGGLES.items()}


def default_store():
    """Every weapon and item at its Free Fire price, all enabled. An organizer unticks what they
    do not want on sale and edits the prices of what remains."""
    return {
        code: {"enabled": True, "price": price}
        for code, _label, price in (STORE_WEAPONS + STORE_ITEMS)
    }


def default_round_economy(rounds):
    """Starting cash for each round of a `rounds`-round set. Free Fire's own curve covers 7 rounds;
    beyond that the last value repeats, which is what the game does for longer sets."""
    last = DEFAULT_ROUND_ECONOMY[max(DEFAULT_ROUND_ECONOMY)]
    return {str(n): DEFAULT_ROUND_ECONOMY.get(n, last) for n in range(1, rounds + 1)}


def default_economy_events():
    return {key: amount for key, (_label, amount) in ECONOMY_EVENTS.items()}


def default_areas(rounds, map_name):
    """One area per round, walking the map's own list and wrapping when there are more rounds than
    areas - the same order the game pre-fills."""
    areas = MAP_AREAS.get(map_name) or []
    if not areas:
        return {}
    return {str(n): areas[(n - 1) % len(areas)][0] for n in range(1, rounds + 1)}


def catalogue_payload():
    """Everything the frontend needs to draw the editor, so no option list is duplicated in TS.
    Served by GET events/cs-room-catalogue/."""
    return {
        "rounds": ROUND_CHOICES,
        "economy": [{"value": v, "label": l} for v, l in ECONOMY_CHOICES],
        "special_mode": [{"value": v, "label": l} for v, l in SPECIAL_MODE_CHOICES],
        "special_airdrop": [{"value": v, "label": l} for v, l in SPECIAL_AIRDROP_CHOICES],
        "hp": HP_CHOICES,
        "ep": EP_CHOICES,
        "movement_speed": MOVEMENT_SPEED_CHOICES,
        "jump_height": JUMP_HEIGHT_CHOICES,
        "environment": [{"value": v, "label": l} for v, l in ENVIRONMENT_CHOICES],
        "maps": [{"value": v, "label": l} for v, l in MAP_CHOICES],
        "map_areas": {
            m: [{"value": v, "label": l} for v, l in areas] for m, areas in MAP_AREAS.items()
        },
        "toggles": [
            {"key": k, "label": label, "default": default}
            for k, (label, default) in TOGGLES.items()
        ],
        "store_weapons": [
            {"code": c, "label": l, "default_price": p} for c, l, p in STORE_WEAPONS
        ],
        "store_items": [
            {"code": c, "label": l, "default_price": p} for c, l, p in STORE_ITEMS
        ],
        "economy_events": [
            {"key": k, "label": label, "default": amount}
            for k, (label, amount) in ECONOMY_EVENTS.items()
        ],
        "presets": [
            # `config` is the PARTIAL patch each mode applies (see PRESET_MODES). It ships to the
            # frontend so the create-event wizard - where the stage does not exist yet and there is
            # nothing to PUT against - can apply a mode locally and still get exactly the values
            # the server would have produced. The saved-scope editor still applies modes
            # server-side; both read this same table.
            {"key": k, "label": p["label"], "description": p["description"],
             "config": p["config"]}
            for k, p in PRESET_MODES.items()
        ],
    }
