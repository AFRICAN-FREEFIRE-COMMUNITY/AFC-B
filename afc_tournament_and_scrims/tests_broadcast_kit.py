"""
Broadcast Kit, client folder pieces (AFC inbox #613 to #618, owner 18 Nov 2026): team flags from an
uploaded empty banner, team billboards by room slot plus a seating sheet, the sky title in EnvWeather,
panel backgrounds and map skins by the game's own names, the name tag choice (team, country, role), and
the name file held to the 13 Sept rule (only what a file that loaded already carried).

Two halves:
  * SimpleTestCase classes need no database: the game's names (client_files.py), the pictures
    (kit_art.py), the name file's bytes.
  * KitZipTests / KitEndpointTests build a real zip from an event (LetterRoomsFixture from
    tests_letter_rooms: stage S1 with lobbies G1 = t0, t1 and G2 = t2, t3).

Run:
    manage.py test afc_tournament_and_scrims.tests_broadcast_kit
"""
import datetime
import io
import json
import shutil
import tempfile
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team

from . import client_files as cf
from . import kit_art
from .broadcast_kit import _name_file_bytes, build_broadcast_kit, build_kit_summary, is_one_room
from .letter_rooms import apply_letters
from .models import TournamentTeam, TournamentTeamMember
from .tests_letter_rooms import LetterRoomsFixture, _u


def _png(size, colour=(200, 30, 60, 255), mode="RGBA"):
    out = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGBA" else colour[:3]).save(out, "PNG")
    return out.getvalue()


def _img(size, colour=(200, 30, 60, 255)):
    return Image.new("RGBA", size, colour)


def _open(raw):
    return Image.open(io.BytesIO(raw)).convert("RGBA")


# ── the game's names ───────────────────────────────────────────────────────────────────────────
class ClientFilesTests(SimpleTestCase):
    def test_flag_letters_are_a_to_x(self):
        self.assertEqual(len(cf.FLAG_LETTERS), 24)
        self.assertIn("A", cf.FLAG_LETTERS)
        self.assertIn("X", cf.FLAG_LETTERS)
        self.assertNotIn("Y", cf.FLAG_LETTERS)
        self.assertNotIn("Z", cf.FLAG_LETTERS)

    def test_sixty_panel_names_each_once_without_capitals(self):
        self.assertEqual(len(cf.HUD_PANELS), 60)
        self.assertEqual(len({n.lower() for n in cf.HUD_PANELS}), 60)

    def test_panel_upload_names_match_without_capitals_and_keep_the_game_spelling(self):
        self.assertEqual(cf.hud_panel_name("killfeedsbackground.PNG"), "KillFeedsBackground")
        self.assertEqual(cf.hud_panel_name("EpBackground.png"), "EPBackground")
        self.assertEqual(cf.hud_panel_name("HP1.png"), "hp1")
        self.assertIsNone(cf.hud_panel_name("NationalBg.png"))   # the game's name is NationBg
        self.assertIsNone(cf.hud_panel_name("hp14.png"))         # no such panel

    def test_map_skins_and_the_amphibian_odd_name(self):
        self.assertEqual(len(cf.MAP_SKINS), 17)
        self.assertEqual(cf.map_skin_file("billboard.png"), "BillBoard.png")
        self.assertEqual(cf.map_skin_file("Vehicle_Amphibian_Sponsor1_D.png"),
                         "Vehicle_Amphibian_Sponsor1_D.png .png")
        self.assertEqual(cf.map_skin_file("Vehicle_Amphibian_Sponsor1_D.png .png"),
                         "Vehicle_Amphibian_Sponsor1_D.png .png")
        self.assertIsNone(cf.map_skin_file("Vehicle_Tank_Sponsor1_D.png"))

    def test_country_table_holds_every_game_country_once(self):
        self.assertEqual(len(cf.GAME_COUNTRIES), 152)  # 154 in the game less A_Default and NationFlag_Other
        self.assertEqual(len(set(cf.GAME_COUNTRIES.values())), 152)
        import pycountry
        for code in cf.GAME_COUNTRIES:
            self.assertIsNotNone(pycountry.countries.get(alpha_2=code), code)

    def test_site_country_to_the_game_spelling(self):
        self.assertEqual(cf.game_country("Nigeria"), "Nigeria")
        self.assertEqual(cf.game_country("South Africa"), "South-Africa")
        self.assertEqual(cf.game_country("Kenya"), "Kneya")             # the game's own typo
        self.assertEqual(cf.game_country("NG"), "Nigeria")
        self.assertIsNone(cf.game_country("Cameroon"))                  # no icon in the game
        self.assertIsNone(cf.game_country(""))
        self.assertIsNone(cf.game_country("Atlantis"))

    def test_role_words(self):
        self.assertEqual(cf.ROLE_WORDS["grenader"], "BOMBER")
        self.assertEqual(set(cf.ROLE_WORDS.values()), {"RUSHER", "SNIPER", "SUPPORT", "BOMBER"})


class NameFileTextTests(SimpleTestCase):
    def test_lowercase_f_and_w_become_capitals(self):
        self.assertEqual(cf.name_file_text("flux wave", 12), "Flux Wave")

    def test_characters_never_seen_are_dropped(self):
        self.assertEqual(cf.name_file_text("Zaza58!", 12), "Zaza")
        self.assertEqual(cf.name_file_text("KIM(X)", 12), "KIMX")

    def test_accents_and_blank_fillers(self):
        self.assertEqual(cf.name_file_text("JoséㅤKING", 12), "Jose KING")

    def test_cut_to_the_limit_and_no_trailing_dot_or_space(self):
        self.assertEqual(cf.name_file_text("ABCDEFGHIJKL MORE", 12), "ABCDEFGHIJKL")
        self.assertEqual(cf.name_file_text("TEAM.", 18), "TEAM")
        self.assertEqual(cf.name_file_text("ABCDEFGHIJK MORE", 12), "ABCDEFGHIJK")

    def test_nothing_left(self):
        self.assertEqual(cf.name_file_text("الف", 12), "")


class NameFileBytesTests(SimpleTestCase):
    def test_shape_of_the_known_good_file(self):
        raw = _name_file_bytes([{"PlayerID": 1, "PlayerNameOverwrite": "A", "PlayerNation": "B",
                                 "Color": "#e6194b"}])
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(list(data), ["PlayerNameList", "TeamRegionList"])
        self.assertEqual(data["TeamRegionList"], [])
        self.assertTrue(raw.startswith(b'{\r\n  "PlayerNameList"'))

    def test_eighteen_known_colours_no_white(self):
        self.assertEqual(len(cf.NAME_FILE_COLOURS), 18)
        self.assertNotIn("#ffffff", cf.NAME_FILE_COLOURS)


# ── the pictures ───────────────────────────────────────────────────────────────────────────────
class KitArtTests(SimpleTestCase):
    def test_flag_keeps_the_banner_size_and_puts_the_logo_where_asked(self):
        banner = _img((512, 1280), (40, 0, 80, 255))
        logo = _img((400, 400), (255, 255, 255, 255))
        out = _open(kit_art.compose_flag(banner, logo, size_pct=50, height_pct=25))
        self.assertEqual(out.size, (512, 1280))
        self.assertEqual(out.getpixel((256, 320))[:3], (255, 255, 255))   # logo centre: 25% down
        self.assertEqual(out.getpixel((256, 900))[:3], (40, 0, 80))       # banner below it
        self.assertEqual(out.getpixel((60, 320))[:3], (40, 0, 80))        # logo box is 256 wide

    def test_flag_never_blows_a_small_logo_up(self):
        banner = _img((512, 1280), (0, 0, 0, 255))
        logo = _img((100, 100), (255, 255, 255, 255))
        out = _open(kit_art.compose_flag(banner, logo, size_pct=90, height_pct=50))
        row = [out.getpixel((x, 640))[0] for x in range(512)]
        self.assertEqual(sum(1 for v in row if v == 255), 120)            # 1.2 x 100, not 461

    def test_billboard_is_1920_by_1080_from_big_art_or_none(self):
        self.assertEqual(_open(kit_art.compose_billboard(None, None, "Team 7")).size, (1920, 1080))
        art = _img((3840, 2160))
        self.assertEqual(_open(kit_art.compose_billboard(art, _img((500, 500)), "NO PRESSURE")).size,
                         (1920, 1080))

    def test_billboard_keeps_small_art_at_its_own_size_cut_to_16_9(self):
        art = _img((1280, 1000))
        self.assertEqual(_open(kit_art.compose_billboard(art, None, "X")).size, (1280, 720))

    def test_billboard_places_the_logo_on_the_side_asked(self):
        logo = _img((800, 800), (255, 255, 255, 255))
        right = _open(kit_art.compose_billboard(None, logo, "SYNDICATE", "right"))
        left = _open(kit_art.compose_billboard(None, logo, "SYNDICATE", "left"))
        centre = _open(kit_art.compose_billboard(None, logo, "SYNDICATE", "centre"))
        # Right: the lockup starts 42% across; the logo is the first 22% of it.
        self.assertEqual(right.getpixel((round(1920 * 0.42) + 100, 540))[:3], (255, 255, 255))
        self.assertEqual(right.getpixel((200, 540))[:3], kit_art.BILLBOARD_PLAIN[:3])
        self.assertEqual(left.getpixel((round(1920 * 0.04) + 100, 540))[:3], (255, 255, 255))
        self.assertEqual(centre.getpixel((960, 400))[:3], (255, 255, 255))

    def test_long_names_still_fit(self):
        out = kit_art.compose_billboard(None, _img((500, 500)), "SAINT VINCENT AND THE GRENADINES ESPORTS")
        self.assertEqual(_open(out).size, (1920, 1080))

    def test_sky_is_the_effect_shape_exactly(self):
        for shape, (_, size) in cf.SKY_EFFECTS.items():
            out = _open(kit_art.fit_sky(_img((4096, 512)), size))
            self.assertEqual(out.size, size, shape)

    def test_plain_png_keeps_the_pixels_and_size(self):
        raw = io.BytesIO()
        Image.new("RGB", (218, 54), (10, 20, 30)).save(raw, "JPEG", quality=95)
        out = _open(kit_art.plain_png(raw.getvalue()))
        self.assertEqual(out.size, (218, 54))
        self.assertIsNone(kit_art.plain_png(b"not a picture"))


# ── a real zip from an event ───────────────────────────────────────────────────────────────────
class KitFixture(LetterRoomsFixture):
    """LetterRoomsFixture plus: t0 letter A (Nigeria), t1 letter Y (South Africa), t2 letter B, t3 no
    letter, in S1; every team a logo; two players each, with UIDs, t0's with roles."""

    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        self.addCleanup(self._media.disable)
        apply_letters(self.event, {self.pk(0): "A", self.pk(1): "Y", self.pk(2): "B"}, [self.s1.stage_id])
        names = ["NO PRESSURE", "Solar flare wave", "TEAM 7", "QRISE ESPORTS"]
        countries = ["Nigeria", "South Africa", "Cameroon", ""]
        self.uids = {}
        for i, tt in enumerate(self.tts):
            team = tt.team
            team.team_name = names[i]
            team.team_logo = SimpleUploadedFile("l%d.png" % i, _png((300, 300), (250, 250, 250, 255)),
                                                content_type="image/png")
            team.save()
            Team.objects.filter(pk=team.pk).update(country=countries[i])
            for j in range(2):
                uid = str(1000000000 + i * 10 + j)
                u = User.objects.create_user(username=("fluxkiller%d%d" % (i, j)) if j == 0 else _u("p"),
                                             email=f"{_u('e')}@t.local", password="pw-strong-9273", uid=uid)
                TournamentTeamMember.objects.create(
                    tournament_team=tt, user=u, event=self.event,
                    in_game_role=(["rusher", "grenader"][j] if i == 0 else None))
                self.uids[(i, j)] = int(uid)
        self.tts = [TournamentTeam.objects.get(pk=tt.pk) for tt in self.tts]

    def zip_of(self, **kw):
        return zipfile.ZipFile(io.BytesIO(build_broadcast_kit(self.event, **kw)))


class KitZipTests(KitFixture):
    def test_one_room_rules(self):
        self.assertTrue(is_one_room(self.event, self.s1, self.g1))
        self.assertFalse(is_one_room(self.event, self.s1, None))     # two lobbies
        self.assertTrue(is_one_room(self.event, self.s2, None))      # one day lobby
        self.assertFalse(is_one_room(self.event, None, None))        # event with stages

    def test_flags_on_the_banner_for_letters_a_to_x_only(self):
        z = self.zip_of(stage=self.s1, group=self.g1, flag_banner_bytes=_png((512, 1280), (40, 0, 80, 255)))
        names = set(z.namelist())
        self.assertIn("FlagLogos/902000034.png", names)               # t0, A
        self.assertNotIn("FlagLogos/902000058.png", names)            # t1, Y: no flag in the game
        self.assertEqual(_open(z.read("FlagLogos/902000034.png")).size, (512, 1280))
        self.assertIn("no flag for letters Y and Z", z.read("HOW_TO_USE.txt").decode())

    def test_no_banner_no_flags(self):
        z = self.zip_of(stage=self.s1, group=self.g1)
        self.assertFalse([n for n in z.namelist() if n.startswith("FlagLogos/")])
        self.assertIn("No team flags", z.read("HOW_TO_USE.txt").decode())

    def test_billboards_and_seating_in_letter_order_for_one_lobby(self):
        z = self.zip_of(stage=self.s1, group=self.g1, billboard_art_bytes=_png((1920, 1080)))
        self.assertEqual(sorted(n for n in z.namelist() if n.startswith("TeamBillboard/")),
                         ["TeamBillboard/1.png", "TeamBillboard/2.png"])
        sheet = z.read("Room seating.txt").decode()
        self.assertIn("\r\n", sheet)
        rows = [ln for ln in sheet.splitlines() if ln[:4].strip().isdigit()]
        self.assertTrue(rows[0].split()[1:3] == ["NO", "PRESSURE"] and " A " in rows[0])
        self.assertIn("none", rows[1])                                 # Y has no flag file

    def test_slots_put_teams_without_a_letter_last(self):
        z = self.zip_of(stage=self.s1, group=self.g2)
        rows = [ln for ln in z.read("Room seating.txt").decode().splitlines() if ln[:4].strip().isdigit()]
        self.assertIn("TEAM 7", rows[0])                               # B
        self.assertIn("QRISE ESPORTS", rows[1])                        # no letter

    def test_two_rooms_no_billboards(self):
        z = self.zip_of(stage=self.s1)
        self.assertFalse([n for n in z.namelist() if n.startswith("TeamBillboard/")])
        self.assertNotIn("Room seating.txt", z.namelist())
        self.assertIn("more than one room", z.read("HOW_TO_USE.txt").decode())

    def test_sky_goes_to_envweather_with_its_switch_folder(self):
        z = self.zip_of(stage=self.s1, group=self.g1, skybox_bytes=_png((2048, 256)), sky_shape="logo")
        names = z.namelist()
        self.assertIn("EnvWeather/SkyboxPicture.png", names)
        self.assertIn("EnvWeather/VFX_VFX_PC_SKYBOX_EWC26/", names)
        self.assertNotIn("PCTexture/SkyboxPicture.png", names)
        self.assertEqual(_open(z.read("EnvWeather/SkyboxPicture.png")).size, (2048, 512))

    def test_panels_and_skins_by_the_game_names(self):
        z = self.zip_of(stage=self.s1, group=self.g1,
                        hud_files=[("KillFeedsBackground", _png((654, 162)))],
                        skin_files=[("Vehicle_Amphibian_Sponsor1_D.png .png", _png((64, 64)))],
                        billboard_bytes=_png((1920, 1080)))
        names = z.namelist()
        self.assertIn("HUD_PIC/KillFeedsBackground.png", names)
        self.assertEqual(_open(z.read("HUD_PIC/KillFeedsBackground.png")).size, (654, 162))
        self.assertIn("PCTexture/Vehicle_Amphibian_Sponsor1_D.png .png", names)
        self.assertIn("PCTexture/Vehicle_Amphibian_Sponsor1_D.png", names)  # spare under the plain name
        self.assertIn("PCTexture/BillBoard.png", names)                     # the older field

    def _rows(self, z):
        raw = z.read("PlayerNameOverwrite.json")
        self.assertIn(b"\r\n", raw)
        return {r["PlayerID"]: r for r in json.loads(raw)["PlayerNameList"]}

    def test_name_file_obeys_the_rule(self):
        rows = self._rows(self.zip_of(stage=self.s1, group=self.g1, caster_name="Caster wolf",
                                      caster_uid="1234567"))
        for r in rows.values():
            self.assertEqual(set(r), {"PlayerID", "PlayerNameOverwrite", "PlayerNation", "Color"})
            self.assertIn(r["Color"], cf.NAME_FILE_COLOURS)
            self.assertLessEqual(len(r["PlayerNameOverwrite"]), 12)
            self.assertLessEqual(len(r["PlayerNation"]), 18)
            self.assertFalse(set(r["PlayerNameOverwrite"] + r["PlayerNation"]) - cf.NAME_FILE_CHARS)
        # "fluxkiller00": the lowercase f, never in a file that loaded, is written as its capital.
        self.assertEqual(rows[self.uids[(0, 0)]]["PlayerNameOverwrite"], "Fluxkiller00")
        self.assertEqual(rows[1234567]["PlayerNameOverwrite"], "Caster WolF")
        self.assertEqual(rows[1234567]["PlayerNation"], "CASTER")
        self.assertNotEqual(rows[1234567]["Color"], "#ffffff")

    def test_team_nation_is_the_cleaned_team_name(self):
        rows = self._rows(self.zip_of(stage=self.s1, group=self.g1))
        self.assertEqual(rows[self.uids[(1, 0)]]["PlayerNation"], "Solar Flare Wave")  # "Solar flare wave"
        self.assertEqual(rows[self.uids[(0, 0)]]["PlayerNation"], "NO PRESSURE")

    def test_country_mode_uses_the_game_spelling(self):
        rows = self._rows(self.zip_of(stage=self.s1, group=self.g1, nation_mode="country"))
        self.assertEqual(rows[self.uids[(0, 0)]]["PlayerNation"], "Nigeria")
        self.assertEqual(rows[self.uids[(1, 0)]]["PlayerNation"], "South-Africa")  # owner's call: kept
        rows = self._rows(self.zip_of(stage=self.s1, group=self.g2, nation_mode="country"))
        self.assertEqual(rows[self.uids[(2, 0)]]["PlayerNation"], "TEAM 7")         # Cameroon: no icon

    def test_role_mode(self):
        rows = self._rows(self.zip_of(stage=self.s1, group=self.g1, nation_mode="role"))
        self.assertEqual(rows[self.uids[(0, 0)]]["PlayerNation"], "RUSHER")
        self.assertEqual(rows[self.uids[(0, 1)]]["PlayerNation"], "BOMBER")
        self.assertEqual(rows[self.uids[(1, 0)]]["PlayerNation"], "Solar Flare Wave")  # no role: team name

    def test_summary_carries_slots_flags_and_nation_values(self):
        rows = build_kit_summary(self.event, self.s1, group=self.g1)
        self.assertEqual([r["slot"] for r in rows], [1, 2])
        self.assertEqual([r["flag_letter"] for r in rows], [True, False])
        self.assertEqual(rows[1]["game_country"], "South-Africa")
        self.assertEqual(rows[0]["players_with_role"], 2)
        self.assertTrue(rows[0]["logo_url"])
        self.assertEqual([r["slot"] for r in build_kit_summary(self.event, self.s1)], [None] * 4)


class KitEndpointTests(KitFixture):
    def setUp(self):
        super().setUp()
        SessionToken.objects.create(user=self.admin, token="kit2-admin-token",
                                    expires_at=timezone.now() + datetime.timedelta(hours=1))
        self.auth = {"HTTP_AUTHORIZATION": "Bearer kit2-admin-token"}
        self.url = f"/events/{self.event.event_id}/broadcast-kit/download/"

    def _post(self, **data):
        base = {"stage_id": str(self.s1.stage_id), "group_id": str(self.g1.group_id)}
        base.update(data)
        return APIClient().post(self.url, base, format="multipart", **self.auth)

    def test_summary_has_one_room_and_the_name_lists(self):
        r = APIClient().get(f"/events/{self.event.event_id}/broadcast-kit/?stage_id={self.s1.stage_id}"
                            f"&group_id={self.g1.group_id}", **self.auth)
        body = r.json()
        self.assertTrue(body["one_room"])
        self.assertEqual(len(body["names"]["hud_panels"]), 60)
        self.assertEqual(len(body["names"]["map_skins"]), 17)

    def test_full_download(self):
        r = self._post(
            nation_mode="country", flag_logo_size="50", flag_logo_height="40", billboard_place="centre",
            sky_shape="title",
            flag_banner=SimpleUploadedFile("banner.png", _png((512, 1280)), "image/png"),
            team_billboard=SimpleUploadedFile("art.png", _png((1920, 1080)), "image/png"),
            skybox=SimpleUploadedFile("sky.png", _png((2048, 256)), "image/png"),
            hud_pics=[SimpleUploadedFile("hp1.png", _png((296, 33)), "image/png"),
                      SimpleUploadedFile("NationalBg.png", _png((10, 10)), "image/png")],
            map_skins=[SimpleUploadedFile("Vehicle_Jeep_Sponsor1_D.png", _png((512, 512), mode="RGB"),
                                          "image/png")],
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        z = zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content) if r.streaming else r.content))
        names = set(z.namelist())
        for n in ("FlagLogos/902000034.png", "TeamBillboard/1.png", "Room seating.txt",
                  "EnvWeather/SkyboxPicture.png", "EnvWeather/VFX_VFX_PC_SKYBOX_FFWS/", "HUD_PIC/hp1.png",
                  "PCTexture/Vehicle_Jeep_Sponsor1_D.png"):
            self.assertIn(n, names)
        self.assertNotIn("HUD_PIC/NationalBg.png", names)
        self.assertIn("NationalBg.png", z.read("HOW_TO_USE.txt").decode())

    def test_a_file_that_is_not_a_picture_is_refused_with_a_code(self):
        r = self._post(flag_banner=SimpleUploadedFile("banner.png", b"<svg></svg>", "image/png"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "not_an_image")
        self.assertEqual(r.json()["field"], "flag_banner")

    def test_unknown_names_do_not_count_against_the_panels(self):
        # 63 files like the MPRO set: the 60 names + 3 misspelt ones. The 60 are used, the 3 listed.
        files = [SimpleUploadedFile(n + ".png", _png((4, 4)), "image/png") for n in cf.HUD_PANELS]
        files += [SimpleUploadedFile(n, _png((4, 4)), "image/png")
                  for n in ("NationalBg.png", "MapTeamBackgrond.png", "ReviveFeedsBackgrond.png")]
        r = self._post(hud_pics=files)
        self.assertEqual(r.status_code, 200, r.content[:200])
        z = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len([n for n in z.namelist() if n.startswith("HUD_PIC/")]), 60)
        self.assertIn("MapTeamBackgrond.png", z.read("HOW_TO_USE.txt").decode())

    def test_more_files_than_django_reads_is_a_coded_refusal(self):
        files = [SimpleUploadedFile("x%d.png" % i, _png((2, 2)), "image/png") for i in range(101)]
        r = self._post(hud_pics=files)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "too_many_files")

    def test_a_signed_out_caller_gets_nothing(self):
        r = APIClient().post(self.url, {}, format="multipart")
        self.assertIn(r.status_code, (401, 403))
