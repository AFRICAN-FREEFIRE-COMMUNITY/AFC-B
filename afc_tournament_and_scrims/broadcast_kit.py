# ── AFC BROADCAST KIT (owner 2026-07-03) ────────────────────────────────────────
# One-click download of the Free Fire PC client-side broadcast files for an event, already
# customized to that event's teams + roster. The operator unzips the result INTO their
# ...\Free Fire_64_Data\ folder and launches the observer client - names, team flags and player
# avatars then show correctly in the caster HUD. See FreeFire_PC_Customization_BuildSheet.md.
#
# WHAT THE ZIP CONTAINS (mirrors the client's _Data layout):
#   PlayerNameOverwrite.json           - PlayerNameList[] {PlayerID(uid), PlayerNameOverwrite(name),
#                                        PlayerNation(team), Color} + TeamRegionList[] per team.
#   HeadPics/<letterId>.png            - each team's LOGO renamed to its assigned letter's HeadPicID,
#   BackPackPics/<letterId>.png          resized per folder (108x130 / 1000x1000 / 1000x1000).
#   GloowallPics/<letterId>.png
#   RolePicture/<uid>.png              - each player's esport image, 108x130,
#   RolePictureHUD/<uid>.png             (and the same in RolePictureHUD).
#   PCTexture/BillBoard.png            - optional, from an uploaded billboard image.
#   PCTexture/SkyboxPicture.png        - optional, from an uploaded skybox image.
#
# LETTER -> HeadPicID map is the owner's fixed assignment (A=902000034 ... Z=902000059); the team's
# assigned_letter (TournamentTeam.assigned_letter, set via the existing Assign-letter UI) picks the id.
#
# CONSUMED BY: views_broadcast_kit.broadcast_kit_download / _preview (gated by _broadcast_gate);
# the frontend BroadcastKitCard mounts on the overlay studio + event edit page.

import io
import json
import zipfile

from PIL import Image

# Letter -> HeadPicID (owner 2026-07-03). A..Z = 902000034..902000059.
LETTER_TO_HEADPIC_ID = {chr(ord("A") + i): str(902000034 + i) for i in range(26)}

# Per-folder target sizes (px), verified against the client's shipped files.
HEADPIC_SIZE = (108, 130)       # HeadPics + RolePicture + RolePictureHUD
LOGO_LARGE_SIZE = (1000, 1000)  # BackPackPics + GloowallPics

# Distinct team colours by index (the client wants a #RRGGBB per team; AFC stores none, so we
# hand out a readable palette the operator can tweak in the JSON afterwards).
_TEAM_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
    "#f032e6", "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9a6324",
    "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1", "#000075",
    "#ff4500", "#00ced1", "#ff1493", "#7fff00", "#1e90ff", "#ffd700",
]


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


def _bytes_to_png(raw, size=None):
    """Uploaded-file bytes -> PNG bytes (optionally resized to fit `size`, contain). None on error."""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    if size:
        img.thumbnail(size, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _event_teams(event, stage=None):
    """Active TournamentTeams for the event (optionally scoped to a stage's competitors)."""
    from .models import TournamentTeam, StageCompetitor
    qs = TournamentTeam.objects.filter(event=event).select_related("team")
    if stage is not None:
        ids = StageCompetitor.objects.filter(stage=stage).values_list("tournament_team_id", flat=True)
        qs = qs.filter(pk__in=list(ids))
    return list(qs)


def build_kit_summary(event, stage=None):
    """Readiness report for the UI: per team, its assigned letter + whether it has a logo, and the
    per-player uid/image coverage. Lets the operator see what will (and won't) be in the zip."""
    from .models import TournamentTeamMember
    from afc_auth.models import profile_of
    rows = []
    for tt in _event_teams(event, stage):
        players, with_uid, with_img = 0, 0, 0
        for m in TournamentTeamMember.objects.filter(tournament_team=tt).select_related("user"):
            players += 1
            if getattr(m.user, "uid", None):
                with_uid += 1
            prof = profile_of(m.user)
            if prof and getattr(prof, "esports_pic", None):
                with_img += 1
        letter = (tt.assigned_letter or "").upper()
        rows.append({
            "tournament_team_id": tt.pk,
            "team_name": tt.team.team_name if tt.team else "?",
            "assigned_letter": tt.assigned_letter or None,
            "headpic_id": LETTER_TO_HEADPIC_ID.get(letter) if letter else None,
            "has_logo": bool(getattr(tt.team, "team_logo", None)) if tt.team else False,
            "players": players,
            "players_with_uid": with_uid,
            "players_with_image": with_img,
        })
    return rows


def build_broadcast_kit(event, stage=None, caster_name="", caster_uid=None,
                        billboard_bytes=None, skybox_bytes=None):
    """Build the whole kit zip (bytes). Best-effort per asset - a team with no logo/letter is
    simply skipped for the flag files but still included in the JSON."""
    from .models import TournamentTeamMember
    from afc_auth.models import profile_of

    teams = _event_teams(event, stage)
    buf = io.BytesIO()
    player_list, team_list, seen_uids = [], [], set()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for idx, tt in enumerate(teams):
            team = tt.team
            team_name = team.team_name if team else "Team %d" % (idx + 1)
            color = _TEAM_PALETTE[idx % len(_TEAM_PALETTE)]
            team_list.append({"TeamID": idx + 1, "TeamRegion": team_name, "Color": color})

            # Flag files: the team LOGO renamed to the assigned letter's HeadPicID, per size.
            letter = (tt.assigned_letter or "").upper()
            hid = LETTER_TO_HEADPIC_ID.get(letter)
            logo = getattr(team, "team_logo", None) if team else None
            if hid and logo:
                small = _fit_png(logo, HEADPIC_SIZE)
                large = _fit_png(logo, LOGO_LARGE_SIZE)
                if small:
                    z.writestr("HeadPics/%s.png" % hid, small)
                if large:
                    z.writestr("BackPackPics/%s.png" % hid, large)
                    z.writestr("GloowallPics/%s.png" % hid, large)

            # Per-player: JSON entry + avatar files (keyed by Free Fire UID).
            for m in TournamentTeamMember.objects.filter(tournament_team=tt).select_related("user"):
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
                player_list.append({
                    "PlayerID": uid_int,
                    "PlayerNameOverwrite": u.username,
                    "PlayerNation": team_name,
                    "Color": color,
                })
                prof = profile_of(u)
                pic = getattr(prof, "esports_pic", None) if prof else None
                if pic:
                    avatar = _fit_png(pic, HEADPIC_SIZE)
                    if avatar:
                        z.writestr("RolePicture/%d.png" % uid_int, avatar)
                        z.writestr("RolePictureHUD/%d.png" % uid_int, avatar)

        # Optional caster entry (needs a UID to key on; name-only is added as a note).
        if caster_name and caster_uid:
            try:
                player_list.append({
                    "PlayerID": int(str(caster_uid).strip()),
                    "PlayerNameOverwrite": caster_name,
                    "PlayerNation": "CASTER",
                    "Color": "#ffffff",
                })
            except (ValueError, TypeError):
                pass

        payload = {"PlayerNameList": player_list, "TeamRegionList": team_list}
        if caster_name and not caster_uid:
            payload["_CasterName"] = caster_name  # informational; the game ignores unknown keys
        z.writestr("PlayerNameOverwrite.json", json.dumps(payload, indent=2, ensure_ascii=False))

        # Optional broadcast art.
        if billboard_bytes:
            png = _bytes_to_png(billboard_bytes)
            if png:
                z.writestr("PCTexture/BillBoard.png", png)
        if skybox_bytes:
            png = _bytes_to_png(skybox_bytes)
            if png:
                z.writestr("PCTexture/SkyboxPicture.png", png)

        z.writestr("HOW_TO_USE.txt",
                   "Unzip these folders + files INTO your Free Fire_64_Data folder\n"
                   "(the folder next to Free Fire_64.exe). Overwrite when asked.\n"
                   "Then launch the observer/spectator client. Files are read once at load,\n"
                   "so drop them BEFORE entering the observer session.\n")

    return buf.getvalue()
