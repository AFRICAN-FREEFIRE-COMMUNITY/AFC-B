# ── The Free Fire observer client's own names and file rules (AFC inbox #613 to #618) ─────────────
# Everything the Broadcast Kit (broadcast_kit.py) must spell EXACTLY the way the game reads it, kept in
# one place so the zip writer, the readiness summary and the tests all use the same table.
#
# WHERE EACH LIST COMES FROM (read on 18 Nov 2026 from the OB55 PC client, tasks/client-folder-names-
# 2026-11-18.md in the WEBSITE folder; every name below was found again in the client's own code and
# data extracts before it was written here):
#   FLAG_LETTERS      the flag table PCFlagPics: avatars 902000034..902000057 = letters A..X. The game
#                     logs "config not found" for 902000058 (Y) and 902000059 (Z): no flag for them.
#   HUD_PANELS        section E, the 60 HUD_PIC names the panel loaders ask for. Matched without capitals
#                     by the game, written in the game's spelling by the kit.
#   MAP_SKINS         section C, the PCTexture re-skins. The amphibian's entry in the game carries a stray
#                     space ("Vehicle_Amphibian_Sponsor1_D.png "), so its file needs the odd name the game
#                     builds from it ("... .png .png").
#   GAME_COUNTRIES    the country icon table PCOBCountryroleUIconfig (152 countries, keyed here by ISO
#                     code; the game's own spellings kept, typos included: Kneya, Cayman-lslands ...).
#   ROLE_WORDS        the same table's 5 role icons. AFC rosters know 4 roles; a grenader is the game's
#                     BOMBER, and RIFLER has no AFC role.
#   SKY_EFFECTS       section D: the folder that switches the sky title effect on, and the banner shape
#                     the effect was made for.
#
# THE NAME FILE RULE (owner, 13 Sept 2026, hard rule; applied to the kit on 18 Nov 2026, inbox #618:
# "All of it, characters too"): PlayerNameOverwrite.json carries only what a file that LOADED already
# carried. The reference is VENT-X-GROW/CLIENT FILES/_bisect/KNOWN_GOOD_REFERENCE_101rows.json: 101 rows,
# the 18 colours below, names up to 12, nation up to 18, the characters in NAME_FILE_CHARS, CRLF, no BOM,
# an empty TeamRegionList. The last refusal to load was the colours. One exception, the owner's choice
# (#618): a country value is the GAME'S OWN spelling from its own table, so it may hold a character the
# reference never had (South-Africa's lowercase f); it still obeys the 18 letter cap.
#
# CONSUMED BY: broadcast_kit.py (zip writer + summary), views_broadcast_kit.py (upload name checks),
# tests_broadcast_kit.py.

import re
import unicodedata

# ── flags ──────────────────────────────────────────────────────────────────────────────────────
FLAG_LETTERS = frozenset(chr(c) for c in range(ord("A"), ord("X") + 1))

# ── HUD_PIC panel backgrounds ──────────────────────────────────────────────────────────────────
HUD_PANELS = (
    "KillFeedsBackground", "HuntFeedsBackground", "ReviveFeedsBackground", "LastTeamBackground",
    "WinRateBackground", "HeadPicBackground", "AroundTeamBackground", "AroundTeamInfoBackground",
    "TeamPlayerBackground1", "TeamPlayerBackground2", "TeamPlayerBackground3", "TeamPlayerBackground4",
    "TeamPlayer1", "TeamPlayer2", "TeamPlayer3", "TeamPlayer4", "TeammatesInfoItemBg", "TeamRegionBg",
    "hp1", "hp2", "hp3", "hp4", "hp5", "hp6", "hp7", "hp8", "hp9", "hp10", "hp12", "hp13",
    "HpBackground", "ShieldBackground", "EPBackground", "NationBg", "WeaponBackground", "ArmorBackground",
    "ItemBackground01", "ItemBackground02", "MapTeamBG", "ScoreboardBackground", "ScoreboardTab",
    "PlayerAccStatsBG", "KillerLeaderBG", "TeamShutDownBG",
    "AchievementKillLeaderBg", "AchievementHighestDamageBg", "AchievementGrenadeKillLeaderBg",
    "AchievementHealMaxBg", "AchievementFarthestBg", "AchievementMoveFarthestBg", "AchievementResureBg",
    "AchievementKillLeaderIcon", "AchievementHighestDamageIcon", "AchievementGrenadeKillLeaderIcon",
    "AchievementHealMaxIcon", "AchievementFarthestIcon", "AchievementMoveFarthestIcon", "AchievementResureIcon",
    "CSBetweenPlayerBackground", "CSBetweenTeamBackground",
)
# Names found in delivered panel sets that the game never reads, with the name it does read.
HUD_MISSPELT = {"nationalbg": "NationBg", "mapteambackgrond": "MapTeamBG",
                "revivefeedsbackgrond": "ReviveFeedsBackground"}

# ── PCTexture map skins: upload name (without .png) -> the file name the game opens ─────────────────
MAP_SKINS = {
    "BillBoard": "BillBoard.png",
    "PCMainBody": "PCMainBody.png", "PCMainBody2": "PCMainBody2.png",
    "PCPickUp_Airdrop_A_D": "PCPickUp_Airdrop_A_D.png", "PCPickUp_Airdrop_B_D": "PCPickUp_Airdrop_B_D.png",
    "PCPickUp_CarePackage": "PCPickUp_CarePackage.png", "PCPony_ADlogo": "PCPony_ADlogo.png",
    "PCShop_Bounty_D": "PCShop_Bounty_D.png", "PCShop_Bounty_N": "PCShop_Bounty_N.png",
    "PCShop_Bounty_S": "PCShop_Bounty_S.png", "Monster_Truck_Sponsor1_D": "Monster_Truck_Sponsor1_D.png",
    "Vehicle_Jeep_Sponsor1_D": "Vehicle_Jeep_Sponsor1_D.png", "Vehicle_MOTO_Sponsor1_D": "Vehicle_MOTO_Sponsor1_D.png",
    "Vehicle_Roadster_Sponsor1_D": "Vehicle_Roadster_Sponsor1_D.png",
    "Vehicle_SUV_Sponsor1_D": "Vehicle_SUV_Sponsor1_D.png", "Vehicle_TUTU_Sponsor1_D": "Vehicle_TUTU_Sponsor1_D.png",
    # The game's own entry is "Vehicle_Amphibian_Sponsor1_D.png " (trailing space): it adds ".png" to a
    # name that does not end in ".png", so this is the file it opens.
    "Vehicle_Amphibian_Sponsor1_D": "Vehicle_Amphibian_Sponsor1_D.png .png",
}

# ── sky title (EnvWeather) ─────────────────────────────────────────────────────────────────────
# shape -> (switch folder the kit writes empty, banner size the effect was made for)
SKY_EFFECTS = {
    "title": ("VFX_VFX_PC_SKYBOX_FFWS", (2048, 256)),
    "logo": ("VFX_VFX_PC_SKYBOX_EWC26", (2048, 512)),
}

# ── PlayerNation values the game draws an icon for ─────────────────────────────────────────────
ROLE_WORDS = {"rusher": "RUSHER", "sniper": "SNIPER", "support": "SUPPORT", "grenader": "BOMBER"}

GAME_COUNTRIES = {
    'AD': 'Andorra', 'AE': 'United-Arab-Emirates', 'AG': 'Antigua-and-Barbuda', 'AI': 'Anguilla',
    'AL': 'Albania', 'AM': 'Armenia', 'AO': 'Angola', 'AR': 'Argentina', 'AT': 'Austria', 'AU': 'Australia',
    'AW': 'Aruba', 'AZ': 'Azerbaijan', 'BA': 'Bosnia-and-Herzegovina', 'BB': 'Barbados', 'BD': 'Bangladesh',
    'BE': 'Belgium', 'BG': 'Bulgaria', 'BH': 'Bahrain', 'BJ': 'Benin', 'BM': 'Bermuda', 'BN': 'Brunei',
    'BO': 'Bolivia', 'BR': 'Brazil', 'BS': 'Bahamas', 'BT': 'Bhutan', 'BY': 'Belarus', 'BZ': 'Belize',
    'CA': 'Canada', 'CH': 'Switzerland', 'CL': 'Chile', 'CO': 'Colombia', 'CR': 'Costa-Rica',
    'CV': 'Cabo Verde', 'CY': 'Cyprus', 'CZ': 'Czech-Republic', 'DE': 'Germany', 'DJ': 'Djibouti',
    'DK': 'Denmark', 'DM': 'Dominica', 'DO': 'Dominican-Republic', 'DZ': 'Algeria', 'EC': 'Ecuador',
    'EE': 'Estonia', 'EG': 'Egypt', 'ER': 'Eritrea', 'ES': 'Spain', 'ET': 'Ethiopia', 'FI': 'Finland',
    'FJ': 'Fiji', 'FM': 'Micronesia', 'FR': 'France', 'GB': 'United-Kingdom', 'GD': 'Grenada', 'GH': 'Ghana',
    'GR': 'Greece', 'GT': 'Guatemala', 'GY': 'Guyana', 'HN': 'Honduras', 'HR': 'Croatia', 'HT': 'Haiti',
    'HU': 'Hungary', 'ID': 'Indonesia', 'IE': 'Ireland', 'IL': 'Israel', 'IN': 'India', 'IQ': 'Iraq',
    'IR': 'Iran', 'IS': 'Iceland', 'IT': 'Italy', 'JM': 'Jamaica', 'JO': 'Jordan', 'JP': 'Japan', 'KE': 'Kneya',
    'KG': 'Kyrgyzstan', 'KH': 'Cambodia', 'KM': 'Comoros', 'KN': 'Saint-Kits-and-Nevis',
    'KR': 'Korea (the Republic of)', 'KW': 'Kuwait', 'KY': 'Cayman-lslands', 'KZ': 'Kazakhstan', 'LA': 'Laos',
    'LB': 'Lebanon', 'LC': 'Saint-Lucia', 'LI': 'Liechtenstein', 'LK': 'Sri-Lanka', 'LT': 'Lithuania',
    'LU': 'Luxembourg', 'LV': 'Latvia', 'LY': 'Libya', 'MA': 'Morocco', 'MD': 'Moldova', 'MG': 'Madagascar',
    'MK': 'Macedonia', 'MM': 'Myanmar', 'MR': 'Mauritania', 'MS': 'Montserrat', 'MT': 'Malta', 'MX': 'Mexico',
    'MY': 'Malaysia', 'MZ': 'Mozambique', 'NG': 'Nigeria', 'NI': 'Nicaragua', 'NL': 'Netherlands',
    'NO': 'Norway', 'NP': 'Nepal', 'NZ': 'New-Zealand', 'OM': 'Oman', 'PA': 'Panama', 'PE': 'Peru',
    'PG': 'Papua-New-Guinea', 'PH': 'Philippines', 'PK': 'Pakistan', 'PL': 'Poland', 'PS': 'Palestine',
    'PT': 'Portugal', 'PW': 'Palau', 'PY': 'Paraguay', 'QA': 'Qatar', 'RO': 'Romania', 'RS': 'Serbia',
    'RU': 'Russia', 'SA': 'Saudi-Arabia', 'SB': 'Solomon-lslands', 'SD': 'Sudan', 'SE': 'Sweden',
    'SG': 'Singapore', 'SI': 'Slovenia', 'SK': 'Slovakia', 'SN': 'Senegal', 'SO': 'Somalia', 'SR': 'Suriname',
    'SV': 'El-Salvador', 'SY': 'Syria', 'TD': 'Chad', 'TH': 'Thailand', 'TJ': 'Tajikistan',
    'TM': 'Turkmenistan', 'TN': 'Tunisia', 'TR': 'Turkey', 'TT': 'Trinidad-and-Tobago', 'TZ': 'Tanzania',
    'UA': 'Ukraine', 'US': 'United-States', 'UY': 'Uruguay', 'UZ': 'Uzbekistan',
    'VC': 'Saint-Vincent-and-the-Grenadines', 'VE': 'Venezuela', 'VI': 'Virgin-lslands-US', 'VN': 'Vietnam',
    'YE': 'Yemen', 'ZA': 'South-Africa',
}

# ── the name file rule (see the header) ───────────────────────────────────────────────────────
NAME_FILE_MAX_ROWS = 101
NAME_MAX = 12
NATION_MAX = 18
NAME_FILE_COLOURS = (  # the 18 colours of the known good file, in the order the kit hands them out
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#bfef45", "#fabed4",
    "#469990", "#dcbeff", "#9a6324", "#800000", "#aaffc3", "#808000", "#000075", "#ff4500", "#00ced1",
)
NAME_FILE_CHARS = frozenset(" -.01234679ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdeghijklmnopqrstuvxyz")
# Lowercase f and w never appeared in a file that loaded, their capitals did: write the capital rather
# than lose the letter. 5, 8 and everything else outside NAME_FILE_CHARS has no stand-in and is dropped.
_CHAR_STAND_INS = {"f": "F", "w": "W"}
# Free Fire names often use the Hangul filler (U+3164) and other blanks as spaces.
_BLANKS = re.compile("[ㅤᅟᅠ -​  　 ﻿]")


def name_file_text(value, limit):
    """`value` reduced to what the name file may carry: plain letters (accents dropped), only the
    characters of the known good file, single spaces, no trailing dot (the checker refuses one), at most
    `limit` characters. May return "" when nothing survives; the caller supplies a fallback."""
    text = _BLANKS.sub(" ", str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    out = []
    for ch in text:
        ch = _CHAR_STAND_INS.get(ch, ch)
        if ch in NAME_FILE_CHARS:
            out.append(ch)
    text = re.sub(" {2,}", " ", "".join(out)).strip()
    text = text[:limit].strip()
    while text.endswith("."):
        text = text[:-1].rstrip()
    return text


def game_country(site_country):
    """The game's spelling of a team's country, or None when the game has no icon for it.

    `site_country` is Team.country (a display name such as "Nigeria" or "South Africa", or an ISO code).
    It is resolved to an ISO alpha 2 code with pycountry (already a dependency: afc_team.views
    _canonical_country), then looked up in GAME_COUNTRIES."""
    raw = (site_country or "").strip()
    if not raw:
        return None
    try:
        import pycountry
        code = pycountry.countries.lookup(raw).alpha_2
    except Exception:
        return None
    return GAME_COUNTRIES.get(code)


def hud_panel_name(filename):
    """The game's spelling of a HUD_PIC panel for an uploaded file name, or None."""
    base = re.sub(r"\.png$", "", (filename or "").strip(), flags=re.I).lower()
    for name in HUD_PANELS:
        if name.lower() == base:
            return name
    return None


def map_skin_file(filename):
    """The PCTexture file name the game opens for an uploaded skin, or None."""
    base = re.sub(r"\.png\s*(\.png)?$", "", (filename or "").strip(), flags=re.I).lower()
    for name, target in MAP_SKINS.items():
        if name.lower() == base:
            return target
    return None
