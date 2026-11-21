# ── AFC BROADCAST KIT (owner 2026-07-03; client folder pieces added 2026-11-18, inbox #618) ────────
# One-click download of the Free Fire PC client-side broadcast files for an event, already
# customized to that event's teams + roster. The operator unzips the result INTO their
# ...\Free Fire_64_Data\ folder and launches the observer client - names, team flags and player
# avatars then show correctly in the caster HUD. See FreeFire_PC_Customization_BuildSheet.md and, for
# every name below, the WEBSITE folder's tasks/client-folder-names-2026-11-18.md (read from the client).
#
# WHAT THE ZIP CONTAINS (mirrors the client's _Data layout):
#   PlayerNameOverwrite.json           - PlayerNameList[] {PlayerID(uid), PlayerNameOverwrite(name),
#                                        PlayerNation, Color} + an EMPTY TeamRegionList, written to the
#                                        13 Sept name file rule (client_files.py header): CRLF, the 18
#                                        known colours, names to 12, nation to 18, known characters only,
#                                        at most 101 rows. PlayerNation is the team name, the team's
#                                        country in the game's spelling (flag icon) or the player's role
#                                        word (role icon), as the organizer picks (nation_mode).
#   HeadPics/<letterId>.png            - each team's LOGO renamed to its assigned letter's HeadPicID,
#   BackPackPics/<letterId>.png          resized per folder (108x130 / 1000x1000 / 1000x1000).
#   GloowallPics/<letterId>.png
#   FlagLogos/<letterId>.png           - the team's logo on the organizer's EMPTY flag banner (#613): the
#                                        in-map flag of the team whose first player wears that avatar.
#                                        Letters A..X only: the game has no flag for Y and Z.
#   TeamBillboard/<slot>.png           - the team's logo + name on the billboard art, named by the team's
#   Room seating.txt                     SLOT in the room (1..N in letter order), plus the sheet that tells
#                                        the operator where to seat each team. Only in a ONE-ROOM zip.
#   RolePicture/<uid>.png              - each player's esport image, 108x130,
#   RolePictureHUD/<uid>.png             (and the same in RolePictureHUD).
#   EnvWeather/SkyboxPicture.png       - optional sky title, fitted to the effect's shape, plus the EMPTY
#   EnvWeather/VFX_VFX_PC_SKYBOX_*/      folder that switches that effect on. (It used to be written to
#                                        PCTexture/SkyboxPicture.png, which the game never reads.)
#   HUD_PIC/<panel>.png                - optional panel backgrounds, by the game's own names.
#   PCTexture/<texture>.png            - optional map skins (billboards, plane, airdrops, vehicles).
#   HOW_TO_USE.txt                     - the steps, and what was left out and why.
#
# LETTER -> HeadPicID map is the owner's fixed assignment (A=902000034 ... Z=902000059); the team's
# letter picks the id. For a stage kit that is the team's EFFECTIVE letter in that stage
# (letter_rooms.effective_letters: the stage's own letter, else the all-stages assigned_letter);
# without a stage it is the all-stages TournamentTeam.assigned_letter (inbox #602).
#
# CONSUMED BY: views_broadcast_kit.broadcast_kit_download / _summary (gated by _broadcast_gate);
# the frontend BroadcastKitCard mounts on the overlay studio + event edit page.

import io
import json
import zipfile

from PIL import Image

from . import client_files as cf
from . import kit_art
from .event_player_image import pic_for_event

# Letter -> HeadPicID (owner 2026-07-03). A..Z = 902000034..902000059.
LETTER_TO_HEADPIC_ID = {chr(ord("A") + i): str(902000034 + i) for i in range(26)}

# Per-folder target sizes (px), verified against the client's shipped files.
HEADPIC_SIZE = (108, 130)       # HeadPics + RolePicture + RolePictureHUD
LOGO_LARGE_SIZE = (1000, 1000)  # BackPackPics + GloowallPics

NATION_MODES = ("team", "country", "role")


def _fit_png(image_field, size):
    """Open an ImageField file, fit it into `size` (contain, transparent pad), return PNG bytes.
    Returns None if the file is missing or not a valid image."""
    try:
        image_field.open("rb")
        raw = image_field.read()
        image_field.close()
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    img.thumbnail(size, Image.LANCZOS)
    # Centre onto a transparent canvas of the exact target size (the game expects fixed dims).
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2), img)
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def _event_teams(event, stage=None, group=None):
    """TournamentTeams for the kit, each with `.kit_letter` set (the letter its flag files use), in SLOT
    order: by letter A..Z, teams without a letter last by name (inbox #617: "assign for me ... so I
    arrange them in room when they join").

    No stage: every registration, all-stages letter. A stage: its competitors, letters effective in
    that stage. A stage + group (inbox #602): only that lobby's teams (letter_rooms.stage_rooms, so a
    round robin day lobby includes its base groups), letters effective in that stage.
    """
    from .models import TournamentTeam, StageCompetitor
    from .letter_rooms import effective_letters, stage_rooms
    qs = TournamentTeam.objects.filter(event=event).select_related("team")
    letters = None
    if stage is not None:
        letters = effective_letters(event, stage)
        if group is not None:
            room = next((r for r in stage_rooms(stage) if r["group_id"] == group.group_id), None)
            ids = room["team_ids"] if room else set()
        else:
            ids = StageCompetitor.objects.filter(stage=stage).values_list("tournament_team_id", flat=True)
        qs = qs.filter(pk__in=list(ids))
    teams = list(qs)
    for tt in teams:
        tt.kit_letter = ((letters.get(tt.pk) if letters is not None else tt.assigned_letter) or "").upper() or None
    teams.sort(key=lambda tt: (tt.kit_letter or "~", (tt.display_name or "").lower(), tt.pk))
    return teams


def is_one_room(event, stage=None, group=None):
    """True when the kit's teams are ONE room, so a team's slot number means something: a lobby was
    picked, or the stage has a single room, or the event has no stages at all."""
    if group is not None:
        return True
    if stage is not None:
        from .letter_rooms import stage_rooms
        return len(stage_rooms(stage)) <= 1
    return not event.stages.exists()


def _team_nation(tt, index):
    """The team name as the name file may carry it (rule in client_files.py), never empty."""
    text = cf.name_file_text(tt.display_name, cf.NATION_MAX)
    if text:
        return text
    return ("TEAM " + tt.kit_letter) if tt.kit_letter else "TEAM"


def _country_nation(tt):
    """The team's country in the game's spelling when the game has an icon for it and it fits the 18
    letter cap, else None. Owner (#618): the game's own spelling is used even where it holds a character
    the known good file never had (South-Africa)."""
    team = tt.team
    value = cf.game_country(getattr(team, "country", "") if team else "")
    return value if value and len(value) <= cf.NATION_MAX else None


def _members(tt):
    from .models import TournamentTeamMember
    return list(TournamentTeamMember.objects.filter(tournament_team=tt).select_related("user"))


def build_kit_summary(event, stage=None, group=None, request=None):
    """Readiness report for the UI: per team, its slot, assigned letter, flag and logo, what will show
    under its players' names, and the per-player uid/image/role coverage. Lets the operator see what
    will (and won't) be in the zip."""
    one_room = is_one_room(event, stage, group)
    rows = []
    for idx, tt in enumerate(_event_teams(event, stage, group)):
        players, with_uid, with_img, with_role = 0, 0, 0, 0
        for m in _members(tt):
            players += 1
            if getattr(m.user, "uid", None):
                with_uid += 1
            # The kept copy counts too (owner 2026-10-25, #400): a removal leaves the event its image.
            if pic_for_event(m.user, event):
                with_img += 1
            if cf.ROLE_WORDS.get(m.in_game_role or ""):
                with_role += 1
        letter = tt.kit_letter or ""
        team = tt.team
        has_logo = bool(getattr(team, "team_logo", None)) if team else False
        logo_url = None
        if has_logo:
            try:
                logo_url = team.team_logo.url
                if request is not None:
                    logo_url = request.build_absolute_uri(logo_url)
            except Exception:
                logo_url = None
        site_country = (getattr(team, "country", "") or "") if team else ""
        rows.append({
            "tournament_team_id": tt.pk,
            # display_name: a ghost has no .team. This feeds a BROADCAST overlay, where "?"
            # would go out on stream instead of the team's name (owner 2026-08-20).
            "team_name": tt.display_name,
            "slot": idx + 1 if one_room else None,
            "assigned_letter": tt.kit_letter,
            "headpic_id": LETTER_TO_HEADPIC_ID.get(letter) if letter else None,
            "flag_letter": letter in cf.FLAG_LETTERS,
            "has_logo": has_logo,
            "logo_url": logo_url,
            # What the name file carries in each nation_mode (the card shows the one picked).
            "nation_team": _team_nation(tt, idx),
            "country": site_country,
            "game_country": _country_nation(tt),
            "players": players,
            "players_with_uid": with_uid,
            "players_with_image": with_img,
            "players_with_role": with_role,
        })
    return rows


def kit_names():
    """The game's own name lists the card matches uploads against (one source: client_files.py)."""
    return {
        "hud_panels": list(cf.HUD_PANELS),
        "hud_misspelt": dict(cf.HUD_MISSPELT),
        "map_skins": list(cf.MAP_SKINS.keys()),
        "flag_letters": sorted(cf.FLAG_LETTERS),
        "name_file_max_rows": cf.NAME_FILE_MAX_ROWS,
    }


def _name_file_bytes(rows):
    """PlayerNameOverwrite.json exactly as the known good file is shaped: indent 2, CRLF, UTF-8 without
    BOM, the PlayerNameList and an EMPTY TeamRegionList (its TeamID is the in-ROOM slot, which a fixed
    numbering mislabels, owner 2026-07-03; the key itself must exist or the client stops loading)."""
    text = json.dumps({"PlayerNameList": rows, "TeamRegionList": []}, indent=2, ensure_ascii=False)
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def _seating_sheet(teams):
    lines = ["Seat each team in its slot as it joins. Slot = the team number the custom room shows.",
             "Then observer settings: Team Billboard On.", "",
             "SLOT  TEAM                          LETTER  FLAG FILE"]
    for i, tt in enumerate(teams):
        letter = tt.kit_letter or "-"
        flag = (LETTER_TO_HEADPIC_ID[tt.kit_letter] + ".png") if tt.kit_letter in cf.FLAG_LETTERS else "none"
        lines.append("%4d  %-30s%-8s%s" % (i + 1, (tt.display_name or "")[:30], letter, flag))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def build_broadcast_kit(event, stage=None, group=None, caster_name="", caster_uid=None,
                        billboard_bytes=None, skybox_bytes=None, *, nation_mode="team",
                        flag_banner_bytes=None, flag_logo_size=62, flag_logo_height=34,
                        billboard_art_bytes=None, billboard_place="right", sky_shape="title",
                        hud_files=(), skin_files=(), extra_notes=()):
    """Build the whole kit zip (bytes). Best-effort per asset - a team with no logo/letter is
    simply skipped for the flag files but still included in the JSON.

    billboard_bytes / skybox_bytes are the card's two older fields (still accepted while an older page
    is open): the billboard becomes the BillBoard.png map skin, the skybox the sky title.
    hud_files: [(game panel name, bytes)]; skin_files: [(PCTexture file name, bytes)], already checked
    against client_files by the view."""
    nation_mode = nation_mode if nation_mode in NATION_MODES else "team"
    teams = _event_teams(event, stage, group)
    one_room = is_one_room(event, stage, group)
    banner = kit_art.open_image(flag_banner_bytes) if flag_banner_bytes else None
    art = kit_art.open_image(billboard_art_bytes) if billboard_art_bytes else None
    notes = list(extra_notes)  # lines for HOW_TO_USE: what was left out and why
    buf = io.BytesIO()
    player_rows, seen_uids, left_out = [], set(), []

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for idx, tt in enumerate(teams):
            team = tt.team
            colour = cf.NAME_FILE_COLOURS[idx % len(cf.NAME_FILE_COLOURS)]
            team_nation = _team_nation(tt, idx)
            country = _country_nation(tt) if nation_mode == "country" else None

            # Flag files: the team LOGO renamed to the assigned letter's HeadPicID, per size.
            letter = tt.kit_letter or ""
            hid = LETTER_TO_HEADPIC_ID.get(letter)
            logo = getattr(team, "team_logo", None) if team else None
            logo_img = kit_art.read_field_image(logo) if logo else None
            if hid and logo:
                small = _fit_png(logo, HEADPIC_SIZE)
                large = _fit_png(logo, LOGO_LARGE_SIZE)
                if small:
                    z.writestr("HeadPics/%s.png" % hid, small)
                if large:
                    z.writestr("BackPackPics/%s.png" % hid, large)
                    z.writestr("GloowallPics/%s.png" % hid, large)
            # FlagLogos (#613): the logo on the organizer's empty banner, letters A..X only.
            if banner is not None and hid and logo_img is not None and letter in cf.FLAG_LETTERS:
                z.writestr("FlagLogos/%s.png" % hid,
                           kit_art.compose_flag(banner, logo_img, flag_logo_size, flag_logo_height))
            # TeamBillboard (#614, #617): one per team, named by its slot in this one room.
            if one_room:
                z.writestr("TeamBillboard/%d.png" % (idx + 1),
                           kit_art.compose_billboard(art, logo_img, tt.display_name, billboard_place))

            # Per-player: JSON entry + avatar files (keyed by Free Fire UID).
            for m in _members(tt):
                u = m.user
                uid = getattr(u, "uid", None)
                if not uid:
                    continue
                try:
                    uid_int = int(str(uid).strip())
                except (ValueError, TypeError):
                    continue
                if uid_int in seen_uids:
                    continue
                seen_uids.add(uid_int)
                if nation_mode == "role":
                    nation = cf.ROLE_WORDS.get(m.in_game_role or "") or team_nation
                else:
                    nation = country or team_nation
                row = {
                    "PlayerID": uid_int,
                    "PlayerNameOverwrite": cf.name_file_text(u.username, cf.NAME_MAX) or "PLAYER",
                    "PlayerNation": nation,
                    "Color": colour,
                }
                if len(player_rows) < cf.NAME_FILE_MAX_ROWS:
                    player_rows.append(row)
                else:
                    left_out.append("%s (%s)" % (u.username, tt.display_name))
                pic = pic_for_event(u, event)
                if pic:
                    avatar = _fit_png(pic, HEADPIC_SIZE)
                    if avatar:
                        z.writestr("RolePicture/%d.png" % uid_int, avatar)
                        z.writestr("RolePictureHUD/%d.png" % uid_int, avatar)

        # Optional caster entry (needs a UID to key on). Never white (the name file rule): the next
        # known colour after the teams'.
        if caster_name and caster_uid:
            try:
                caster_row = {
                    "PlayerID": int(str(caster_uid).strip()),
                    "PlayerNameOverwrite": cf.name_file_text(caster_name, cf.NAME_MAX) or "CASTER",
                    "PlayerNation": "CASTER",
                    "Color": cf.NAME_FILE_COLOURS[len(teams) % len(cf.NAME_FILE_COLOURS)],
                }
                if caster_row["PlayerID"] not in seen_uids:
                    if len(player_rows) < cf.NAME_FILE_MAX_ROWS:
                        player_rows.append(caster_row)
                    else:
                        left_out.append("%s (caster)" % caster_name)
            except (ValueError, TypeError):
                pass
        elif caster_name:
            notes.append("Caster %s has no UID, so the name file has no row for them." % caster_name)
        if left_out:
            notes.append("The name file holds at most %d rows (the most a file that loaded had); left out: %s. "
                         "Pick one lobby to get a smaller kit." % (cf.NAME_FILE_MAX_ROWS, ", ".join(left_out)))
        z.writestr("PlayerNameOverwrite.json", _name_file_bytes(player_rows))

        if one_room and teams:
            z.writestr("Room seating.txt", _seating_sheet(teams))
        elif not one_room:
            notes.append("No team billboards or seating sheet: this zip covers more than one room. Pick one "
                         "lobby to get them.")
        if banner is None:
            notes.append("No team flags: no empty flag banner was added.")
        no_flag = [tt.display_name for tt in teams if tt.kit_letter and tt.kit_letter not in cf.FLAG_LETTERS]
        if banner is not None and no_flag:
            notes.append("No flag for %s: the game has no flag for letters Y and Z." % ", ".join(no_flag))

        # Sky title: EnvWeather/SkyboxPicture.png + the empty folder that switches the effect on.
        sky = kit_art.open_image(skybox_bytes) if skybox_bytes else None
        if sky is not None:
            folder, size = cf.SKY_EFFECTS.get(sky_shape) or cf.SKY_EFFECTS["title"]
            z.writestr("EnvWeather/SkyboxPicture.png", kit_art.fit_sky(sky, size))
            z.writestr(zipfile.ZipInfo("EnvWeather/%s/" % folder), b"")

        # Panel backgrounds and map skins: the organizer's own pixels, by the game's names.
        for name, raw in hud_files:
            png = kit_art.plain_png(raw)
            if png:
                z.writestr("HUD_PIC/%s.png" % name, png)
        skins = dict(skin_files)
        if billboard_bytes and "BillBoard.png" not in skins:
            skins["BillBoard.png"] = billboard_bytes
        for target, raw in skins.items():
            png = kit_art.plain_png(raw)
            if png:
                z.writestr("PCTexture/%s" % target, png)
                # The amphibian's odd name is read from the game's table, not yet seen working in a
                # room: a spare copy under the plain name costs nothing if the game trims the space
                # (the MPRO semis set, Revision 6 by videos-e2, ships the same pair).
                if target.endswith(".png .png"):
                    z.writestr("PCTexture/%s" % target[: -len(" .png")], png)

        # The folders this zip writes, for the "delete them first" step (read off the zip itself, so the list
        # is always exactly what this kit carries).
        folders = sorted({n.split("/", 1)[0] for n in z.namelist() if "/" in n})
        z.writestr("HOW_TO_USE.txt", _how_to_use(nation_mode, one_room, notes, folders))

    return buf.getvalue()


def _how_to_use(nation_mode, one_room, notes, folders=()):
    # DELETE FIRST, THEN COPY (AFC inbox #621, owner 2026-11-19 "Build it on a branch"). "Overwrite when asked"
    # left an earlier kit's files in place, so the game's HUD_PIC held 25 old panels mixed with 38 new ones on
    # the semi finals' Day 3. None of these folders comes with the game (checked against the stock OB55 client
    # zip on 19 Nov 2026: it ships none of HUD_PIC, PCTexture, HeadPics, GloowallPics, BackPackPics, FlagLogos,
    # RolePicture, RolePictureHUD, TeamBillboard, EnvWeather), so deleting them removes only earlier kits' files.
    lines = [
        "Unzip these folders + files INTO your Free Fire_64_Data folder",
        "(the folder next to Free Fire_64.exe).",
    ]
    if folders:
        lines += [
            "FIRST delete these folders from Free Fire_64_Data if they are there: " + ", ".join(folders) + ".",
            "None of them comes with the game; they only hold an earlier kit's pictures, and copying over them",
            "leaves old files mixed in with these. Then copy. PlayerNameOverwrite.json replaces the old one.",
        ]
    lines += [
        "Close AFC Capture while copying, then start Free Fire, then Capture.",
        "Files are read once at load, so drop them BEFORE entering the observer session.",
        "",
        "What each part does:",
        "- PlayerNameOverwrite.json: every player's name, colour and what shows under the name.",
        "- HeadPics, GloowallPics, BackPackPics: the team logo, by the team's letter.",
        "- FlagLogos: the team's flag on a pole in the map (Bermuda, Purgatory, Kalahari, Alpine, NeXTerra,",
        "  Solara). The game finds it by the avatar the team's FIRST player wears: set it to the team's letter.",
        "- RolePicture, RolePictureHUD: player photos, by Free Fire UID.",
    ]
    if one_room:
        lines += [
            "- TeamBillboard: after a kill the map billboards show the killing team's picture. Switch on",
            "  observer settings > Team Billboard, and seat each team in its slot (Room seating.txt).",
        ]
    lines += [
        "- EnvWeather: the title in the sky; the empty VFX_VFX_PC_SKYBOX_ folder switches the effect on.",
        "- HUD_PIC: panel backgrounds (each picture is stretched into its panel).",
        "- PCTexture: map skins (billboards, plane, airdrops, vehicles).",
    ]
    if nation_mode == "country":
        lines += ["", "Under each name: the team's country, spelled the way the game spells it, so the game "
                  "shows its flag icon. A country the game does not know shows the team name.",
                  "Load test: some country spellings (South-Africa, Sweden, Norway, Kuwait, New-Zealand) hold a "
                  "letter no name file that loaded has held yet. Open one custom room with this kit before the "
                  "broadcast and check the names show."]
    elif nation_mode == "role":
        lines += ["", "Under each name: the player's role word (RUSHER, SNIPER, BOMBER, SUPPORT), so the game "
                  "shows its role icon. A player with no role shows the team name."]
    if notes:
        lines += ["", "Left out of this kit:"] + ["- " + n for n in notes]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
