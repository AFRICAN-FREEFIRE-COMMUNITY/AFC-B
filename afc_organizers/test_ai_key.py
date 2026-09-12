"""
afc_organizers/test_ai_key.py - OCR on the organization's own AI key (owner 2026-09-12).

What must stay true, and the test that holds it:
    - the key is sealed at rest, opens back, and never leaves any endpoint            (SealTests)
    - the OpenAI-compatible and Anthropic adapters turn recorded replies into the
      canonical draft: plain JSON, fenced JSON, a chatty preamble, a bare list;
      a malformed reply is re-asked once with the nudge, then refused in words;
      the provider's own error message comes through, the key never does        (AdapterTests)
    - whose key pays, in the owner's order: staff -> AFC; org key -> org; the one
      free read -> AFC and spent exactly once under contention; then refused;
      AFC-run events -> AFC; a switched-off org -> refused                          (RoutingTests)
    - every AI read writes an OcrUsage row; three failures in a row on the org key
      notify the org owner with the provider's message                              (UsageTests)
    - the endpoints: owner / manage member / platform admin pass, a sub-organizer
      without can_manage_members is 403; PUT tests before saving and refuses a
      failing key; the test endpoint answers the provider's message verbatim and
      is limited to 5 per 10 min; DELETE disconnects; history rows are written;
      the admin list never carries the key; allowance + switch endpoints work      (EndpointTests)
    - the event OCR upload answers 402 with the sentence and the slug when a key
      is required                                                                   (UploadGateTests)

Run: python manage.py test afc_organizers.test_ai_key
"""
import datetime
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User, UserProfile
from afc_auth.secret_box import open_sealed, seal
from afc_ocr.models import OcrUsage
from afc_ocr.services import extract
from afc_ocr.services.providers import ProviderError, anthropic, openai_compat, parse_placements
from afc_tournament_and_scrims.models import Event

from .models import Organization, OrganizationAiKey, OrganizationAiKeyEvent, OrganizationMember


# ── fixtures ────────────────────────────────────────────────────────────────────────────────
def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(
        user=u, token=f"tok_{username}"[:32],
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )
    return u, tok.token


def _org(slug, owner):
    org = Organization.objects.create(slug=slug, name=slug.title(), created_by=owner)
    OrganizationMember.objects.create(organization=org, user=owner, role="owner", status="active")
    return org


def _event(creator, org=None):
    return Event.objects.create(
        event_name="Key Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=16, event_mode="single",
        start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=4),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=2),
        number_of_stages=1, creator=creator, is_public=True, is_draft=False, organization=org,
    )


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


GOOD = {"placements": [{"placement": 1, "team_name": "AFC TEST ONE", "kills": 9, "players": []},
                       {"placement": 2, "team_name": "AFC TEST TWO", "kills": 6, "players": []}]}


def openai_reply(text, status=200):
    return FakeResponse(status, {"choices": [{"message": {"content": text}}]})


def anthropic_reply(text, status=200):
    return FakeResponse(status, {"content": [{"type": "text", "text": text}]})


def _post(token, path, body=None):
    return Client().post(path, json.dumps(body or {}), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _put(token, path, body=None):
    return Client().put(path, json.dumps(body or {}), content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {token}")


def _get(token, path):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


def _delete(token, path):
    return Client().delete(path, HTTP_AUTHORIZATION=f"Bearer {token}")


# ── the seal ────────────────────────────────────────────────────────────────────────────────
class SealTests(TestCase):
    def test_round_trip_and_garbage(self):
        sealed = seal("sk-test-1234abcd")
        self.assertNotIn("sk-test", sealed)
        self.assertEqual(open_sealed(sealed), "sk-test-1234abcd")
        self.assertEqual(open_sealed("not-a-token"), "")
        self.assertEqual(open_sealed(""), "")

    def test_model_stores_last_four_only_in_the_clear(self):
        owner, _ = _user("seal_owner")
        org = _org("seal-org", owner)
        k = OrganizationAiKey(organization=org, provider="openai", model="gpt-4.1-mini")
        k.set_key("  sk-abcdefghijklmnopqrstuvwxyz0042  ")
        k.save()
        k = OrganizationAiKey.objects.get(pk=k.pk)
        self.assertEqual(k.last_four, "0042")
        self.assertNotIn("abcdefghijklmnop", k.key_sealed)
        self.assertEqual(k.get_key(), "sk-abcdefghijklmnopqrstuvwxyz0042")


# ── the adapters ────────────────────────────────────────────────────────────────────────────
class AdapterTests(TestCase):
    def test_parse_accepts_the_shapes_models_produce(self):
        j = json.dumps(GOOD)
        self.assertEqual(parse_placements(j), GOOD)
        self.assertEqual(parse_placements("```json\n" + j + "\n```"), GOOD)
        self.assertEqual(parse_placements("Here is the JSON you asked for:\n" + j), GOOD)
        self.assertEqual(parse_placements(json.dumps(GOOD["placements"])), GOOD)
        for bad in ("", "no json here", '{"rows": []}', '{"placements": [1, 2]}'):
            with self.assertRaises(ValueError):
                parse_placements(bad)

    def test_openai_compat_reads_and_retries_once_then_refuses(self):
        replies = [openai_reply("Sure! " + json.dumps(GOOD))]
        with patch.object(openai_compat.requests, "post", side_effect=replies) as post:
            out = openai_compat.read(b"img", "image/png", "prompt", api_key="sk-secret", model="gpt-4.1-mini",
                                     base_url="https://api.openai.com/v1")
        self.assertEqual(out, GOOD)
        sent = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-secret")
        self.assertEqual(sent["json"]["model"], "gpt-4.1-mini")
        # malformed twice: the second ask carries the nudge, then a ProviderError in words
        with patch.object(openai_compat.requests, "post", side_effect=[openai_reply("I cannot"), openai_reply("still no")]) as post:
            with self.assertRaises(ProviderError) as ctx:
                openai_compat.read(b"img", "image/png", "prompt", api_key="k", model="m", base_url="https://x/v1")
        self.assertEqual(post.call_count, 2)
        self.assertIn("first character of your reply must be {", post.call_args.kwargs["json"]["messages"][0]["content"][0]["text"])
        self.assertIn("did not return the standings as JSON", ctx.exception.message)

    def test_openai_compat_surfaces_the_providers_message_without_the_key(self):
        err = FakeResponse(401, {"error": {"message": "Incorrect API key provided: sk-abc***. You can find your API key at platform.openai.com.", "type": "invalid_request_error"}})
        with patch.object(openai_compat.requests, "post", return_value=err):
            with self.assertRaises(ProviderError) as ctx:
                openai_compat.read(b"img", "image/png", "prompt", api_key="sk-realkey-9999", model="m", base_url="https://x/v1")
        self.assertIn("Incorrect API key provided", ctx.exception.message)
        self.assertNotIn("sk-realkey-9999", ctx.exception.message)
        self.assertEqual(ctx.exception.status, 401)

    def test_openai_compat_timeout_and_shape_errors_read_in_words(self):
        with patch.object(openai_compat.requests, "post", side_effect=openai_compat.requests.Timeout()):
            with self.assertRaises(ProviderError) as ctx:
                openai_compat.read(b"img", "image/png", "p", api_key="k", model="m", base_url="https://x/v1")
        self.assertIn("did not answer within", ctx.exception.message)
        with patch.object(openai_compat.requests, "post", return_value=FakeResponse(200, {"unexpected": True})):
            with self.assertRaises(ProviderError) as ctx:
                openai_compat.read(b"img", "image/png", "p", api_key="k", model="m", base_url="https://x/v1")
        self.assertIn("shape this reader does not understand", ctx.exception.message)

    def test_anthropic_reads_and_surfaces_errors(self):
        with patch.object(anthropic.requests, "post", return_value=anthropic_reply("```json\n" + json.dumps(GOOD) + "\n```")) as post:
            out = anthropic.read(b"img", "image/png", "prompt", api_key="sk-ant-secret", model="claude-haiku-4-5")
        self.assertEqual(out, GOOD)
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "sk-ant-secret")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"][0]["type"], "image")
        err = FakeResponse(400, {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}})
        with patch.object(anthropic.requests, "post", return_value=err):
            with self.assertRaises(ProviderError) as ctx:
                anthropic.read(b"img", "image/png", "p", api_key="k", model="m")
        self.assertIn("credit balance is too low", ctx.exception.message)


# ── routing: whose key pays ─────────────────────────────────────────────────────────────────
@override_settings(GEMINI_API_KEY="afc-gemini-key", OCR_LOCAL_FIRST=False, OCR_GEMINI_FALLBACK=True)
class RoutingTests(TestCase):
    def setUp(self):
        self.owner, self.owner_tok = _user("rt_owner")
        self.org = _org("rt-org", self.owner)
        self.admin, self.admin_tok = _user("rt_admin", role="admin")

    def test_afc_run_event_and_staff_use_afc(self):
        c = extract.resolve_credentials(None, self.owner)
        self.assertEqual((c.paid_by, c.api_key), ("afc", "afc-gemini-key"))
        c = extract.resolve_credentials(self.org, self.admin)
        self.assertEqual(c.paid_by, "afc")
        self.org.refresh_from_db()
        self.assertEqual(self.org.ocr_free_reads_left, 1, "staff never spend the org's free read")

    def test_org_key_wins_over_the_free_read(self):
        k = OrganizationAiKey(organization=self.org, provider="openai", model="gpt-4.1-mini")
        k.set_key("sk-org-key-7777")
        k.save()
        c = extract.resolve_credentials(self.org, self.owner)
        self.assertEqual((c.provider, c.model, c.api_key, c.paid_by), ("openai", "gpt-4.1-mini", "sk-org-key-7777", "org"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.ocr_free_reads_left, 1)

    def test_free_read_then_refused(self):
        c = extract.resolve_credentials(self.org, self.owner)
        self.assertEqual((c.paid_by, c.api_key), ("afc_free", "afc-gemini-key"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.ocr_free_reads_left, 0)
        with self.assertRaises(extract.OcrKeyRequired) as ctx:
            extract.resolve_credentials(self.org, self.owner)
        self.assertIn("Connect your own AI key", str(ctx.exception))
        self.assertIs(ctx.exception.organization, self.org)

    def test_switched_off_org_is_refused_even_with_a_key(self):
        k = OrganizationAiKey(organization=self.org, provider="openai", model="m")
        k.set_key("sk-org-key-7777")
        k.save()
        self.org.ocr_disabled = True
        self.org.save(update_fields=["ocr_disabled"])
        with self.assertRaises(extract.OcrKeyRequired):
            extract.resolve_credentials(self.org, self.owner)

    def test_a_key_that_no_longer_opens_falls_back_to_the_free_read(self):
        OrganizationAiKey.objects.create(organization=self.org, provider="openai", model="m",
                                         key_sealed="garbage", last_four="xxxx")
        c = extract.resolve_credentials(self.org, self.owner)
        self.assertEqual(c.paid_by, "afc_free")

    def test_extract_rows_on_the_org_key_records_usage_and_labels_the_engine(self):
        k = OrganizationAiKey(organization=self.org, provider="openai", model="gpt-4.1-mini")
        k.set_key("sk-org-key-7777")
        k.save()
        event = _event(self.owner, self.org)
        with patch.object(openai_compat.requests, "post", return_value=openai_reply(json.dumps(GOOD))):
            out, engine = extract.extract_rows(b"img", "image/png", "team", org=self.org, actor=self.owner, event=event)
        self.assertEqual(out["placements"], GOOD["placements"])
        self.assertEqual(out["_paid_by"], "org")
        self.assertEqual(engine, "openai:gpt-4.1-mini")
        row = OcrUsage.objects.get()
        self.assertEqual((row.organization_id, row.event_id, row.paid_by, row.provider, row.ok), (self.org.pk, event.pk, "org", "openai", True))
        self.assertGreater(row.cost_estimate_usd, 0)
        k.refresh_from_db()
        self.assertTrue(k.last_test_ok)

    def test_three_failures_notify_the_owner_once(self):
        k = OrganizationAiKey(organization=self.org, provider="openai", model="m")
        k.set_key("sk-org-key-7777")
        k.save()
        err = FakeResponse(402, {"error": {"message": "You exceeded your current quota"}})
        for _ in range(4):
            with patch.object(openai_compat.requests, "post", return_value=err):
                with self.assertRaises(ProviderError):
                    extract.extract_rows(b"img", "image/png", "team", org=self.org, actor=self.owner)
        self.assertEqual(OcrUsage.objects.filter(ok=False).count(), 4)
        notes = Notifications.objects.filter(notification_type="ocr_key_failing", user=self.owner)
        self.assertEqual(notes.count(), 1)
        self.assertIn("exceeded your current quota", notes.first().message)
        k.refresh_from_db()
        self.assertEqual(k.consecutive_failures, 4)
        self.assertIn("exceeded your current quota", k.last_error)


@override_settings(GEMINI_API_KEY="afc-gemini-key")
class FreeReadRaceTests(TransactionTestCase):
    """Two uploads at once with one free read left: exactly one gets it."""

    def test_only_one_of_two_concurrent_reads_spends_the_free_read(self):
        import threading
        from django.db import connection
        owner, _ = _user("race_owner")
        org = _org("race-org", owner)
        results = []

        def go():
            try:
                c = extract.resolve_credentials(org, owner)
                results.append(c.paid_by)
            except extract.OcrKeyRequired:
                results.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["afc_free", "refused"])
        org.refresh_from_db()
        self.assertEqual(org.ocr_free_reads_left, 0)


# ── the endpoints ───────────────────────────────────────────────────────────────────────────
@override_settings(GEMINI_API_KEY="afc-gemini-key")
class EndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.owner_tok = _user("ep_owner")
        self.org = _org("ep-org", self.owner)
        self.manager, self.manager_tok = _user("ep_manager")
        OrganizationMember.objects.create(organization=self.org, user=self.manager, role="sub_organizer",
                                          status="active", can_manage_members=True)
        self.helper, self.helper_tok = _user("ep_helper")
        OrganizationMember.objects.create(organization=self.org, user=self.helper, role="sub_organizer",
                                          status="active", can_upload_results=True)
        self.admin, self.admin_tok = _user("ep_admin", role="admin")
        from afc_auth.models import Roles, UserRoles
        role, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=role)
        self.url = f"/organizers/organization/{self.org.slug}/ai-key/"

    def test_gate(self):
        self.assertEqual(_get(self.owner_tok, self.url).status_code, 200)
        self.assertEqual(_get(self.manager_tok, self.url).status_code, 200)
        self.assertEqual(_get(self.admin_tok, self.url).status_code, 200)
        r = _get(self.helper_tok, self.url)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertIn("manages the organization", r.json()["message"])
        self.assertEqual(Client().get(self.url).status_code, 400)

    def test_get_carries_the_providers_in_the_owners_order_and_no_key(self):
        body = _get(self.owner_tok, self.url).json()
        self.assertIsNone(body["key"])
        self.assertEqual([p["id"] for p in body["providers"]],
                         ["gemini", "openai", "anthropic", "openrouter", "groq", "mistral", "xai", "custom"])
        self.assertEqual(body["usage"]["free_reads_left"], 1)
        self.assertTrue(all(p["steps"] for p in body["providers"]))

    def test_put_tests_first_and_refuses_a_failing_key(self):
        err = FakeResponse(401, {"error": {"message": "Incorrect API key provided"}})
        with patch.object(openai_compat.requests, "post", return_value=err):
            r = _put(self.owner_tok, self.url, {"provider": "openai", "key": "sk-badkey-000000"})
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("Incorrect API key provided", r.json()["message"])
        self.assertFalse(OrganizationAiKey.objects.filter(organization=self.org).exists())
        ev = OrganizationAiKeyEvent.objects.get(organization=self.org)
        self.assertEqual((ev.action, ev.last_four), ("tested", "0000"))

    def test_put_saves_a_working_key_and_never_returns_it(self):
        with patch.object(openai_compat.requests, "post", return_value=openai_reply(json.dumps(GOOD))) as post:
            r = _put(self.manager_tok, self.url, {"provider": "openai", "key": "sk-goodkey-424242"})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("Works. Read 2 rows", body["message"])
        self.assertEqual(body["key"]["last_four"], "4242")
        self.assertEqual(body["key"]["model"], "gpt-4.1-mini")
        self.assertEqual(body["key"]["added_by"], "ep_manager")
        self.assertNotIn("sk-goodkey", r.content.decode())
        self.assertNotIn("key_sealed", r.content.decode())
        # the sample really went to the provider as an image
        content = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        k = OrganizationAiKey.objects.get(organization=self.org)
        self.assertEqual(k.get_key(), "sk-goodkey-424242")
        self.assertEqual(OrganizationAiKeyEvent.objects.filter(organization=self.org, action="connected").count(), 1)
        # GET now shows it, without the key; a second PUT is a "changed" event
        body = _get(self.owner_tok, self.url).json()
        self.assertEqual(body["key"]["last_four"], "4242")
        self.assertNotIn("sk-goodkey", json.dumps(body))
        with patch.object(openai_compat.requests, "post", return_value=openai_reply(json.dumps(GOOD))):
            r = _put(self.owner_tok, self.url, {"provider": "openrouter", "key": "sk-or-newkey-1111", "model": "google/gemini-2.5-flash"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(OrganizationAiKeyEvent.objects.filter(organization=self.org, action="changed").count(), 1)
        self.assertEqual(OrganizationAiKey.objects.get(organization=self.org).provider, "openrouter")

    def test_put_validation(self):
        self.assertEqual(_put(self.owner_tok, self.url, {"provider": "nope", "key": "sk-xxxxxxxx"}).status_code, 400)
        self.assertEqual(_put(self.owner_tok, self.url, {"provider": "openai", "key": "short"}).status_code, 400)
        r = _put(self.owner_tok, self.url, {"provider": "custom", "key": "sk-xxxxxxxxxx", "model": "m"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("base URL", r.json()["message"])

    def test_test_endpoint_verbatim_error_and_rate_limit(self):
        err = FakeResponse(429, {"error": {"message": "Rate limit reached for gpt-4.1-mini"}})
        with patch.object(openai_compat.requests, "post", return_value=err):
            r = _post(self.owner_tok, self.url + "test/", {"provider": "openai", "key": "sk-testkey-333333"})
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(r.json()["ok"])
        self.assertIn("Rate limit reached", r.json()["message"])
        # nothing saved by a test
        self.assertFalse(OrganizationAiKey.objects.filter(organization=self.org).exists())
        # 5 per 10 minutes per organization
        with patch.object(openai_compat.requests, "post", return_value=openai_reply(json.dumps(GOOD))):
            for _ in range(4):
                self.assertEqual(_post(self.owner_tok, self.url + "test/", {"provider": "openai", "key": "sk-testkey-333333"}).status_code, 200)
            r = _post(self.owner_tok, self.url + "test/", {"provider": "openai", "key": "sk-testkey-333333"})
        self.assertEqual(r.status_code, 429, r.content)

    def test_test_endpoint_on_the_saved_key(self):
        k = OrganizationAiKey(organization=self.org, provider="anthropic", model="claude-haiku-4-5")
        k.set_key("sk-ant-saved-9999")
        k.save()
        with patch.object(anthropic.requests, "post", return_value=anthropic_reply(json.dumps(GOOD))) as post:
            r = _post(self.owner_tok, self.url + "test/", {})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "sk-ant-saved-9999")
        k.refresh_from_db()
        self.assertTrue(k.last_test_ok)
        self.assertIsNotNone(k.last_tested_at)

    def test_delete_disconnects_and_logs(self):
        k = OrganizationAiKey(organization=self.org, provider="groq", model="m")
        k.set_key("gsk_abcdefgh")
        k.save()
        r = _delete(self.owner_tok, self.url)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(OrganizationAiKey.objects.filter(organization=self.org).exists())
        ev = OrganizationAiKeyEvent.objects.get(organization=self.org, action="disconnected")
        self.assertEqual((ev.provider, ev.last_four), ("groq", "efgh"))
        self.assertEqual(_delete(self.owner_tok, self.url).status_code, 404)
        hist = _get(self.owner_tok, self.url + "history/").json()["events"]
        self.assertEqual(hist[0]["action"], "disconnected")

    def test_usage_and_admin_endpoints(self):
        OcrUsage.objects.create(organization=self.org, paid_by="org", provider="openai", model="m", ok=True, cost_estimate_usd=0.002)
        OcrUsage.objects.create(organization=self.org, paid_by="org", provider="openai", model="m", ok=False, error="boom")
        OcrUsage.objects.create(organization=None, paid_by="afc", provider="gemini", model="g", ok=True, cost_estimate_usd=0.001)
        u = _get(self.owner_tok, self.url + "usage/").json()
        self.assertEqual((u["month"]["reads"], u["month"]["ok"]), (2, 1))
        self.assertAlmostEqual(u["month"]["estimated_usd"], 0.002)
        self.assertEqual(u["last_read_error"], "boom")
        # admin list: no key material, the numbers, AFC's own use
        self.assertEqual(_get(self.owner_tok, "/organizers/admin/ai-keys/").status_code, 403)
        k = OrganizationAiKey(organization=self.org, provider="openai", model="m")
        k.set_key("sk-adminlist-5555")
        k.save()
        body = _get(self.admin_tok, "/organizers/admin/ai-keys/").json()
        row = next(r for r in body["organizations"] if r["slug"] == self.org.slug)
        self.assertEqual((row["provider"], row["last_four"], row["reads_month"], row["free_reads_left"]), ("openai", "5555", 2, 1))
        self.assertNotIn("sk-adminlist", json.dumps(body))
        self.assertEqual(body["afc_key_month"]["reads"], 1)
        # allowance + switch
        r = _put(self.admin_tok, f"/organizers/admin/ai-keys/{self.org.pk}/allowance/", {"free_reads_left": 5})
        self.assertEqual(r.status_code, 200, r.content)
        self.org.refresh_from_db()
        self.assertEqual(self.org.ocr_free_reads_left, 5)
        self.assertEqual(_put(self.admin_tok, f"/organizers/admin/ai-keys/{self.org.pk}/allowance/", {"free_reads_left": -1}).status_code, 400)
        self.assertEqual(_put(self.owner_tok, f"/organizers/admin/ai-keys/{self.org.pk}/allowance/", {"free_reads_left": 5}).status_code, 403)
        r = _put(self.admin_tok, f"/organizers/admin/ai-keys/{self.org.pk}/ocr-disabled/", {"disabled": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.org.refresh_from_db()
        self.assertTrue(self.org.ocr_disabled)
        actions = set(OrganizationAiKeyEvent.objects.filter(organization=self.org).values_list("action", flat=True))
        self.assertTrue({"allowance", "disabled"} <= actions)


# ── the upload paths answer 402 ─────────────────────────────────────────────────────────────
@override_settings(GEMINI_API_KEY="afc-gemini-key", OCR_LOCAL_FIRST=False)
class UploadGateTests(TestCase):
    def test_key_required_response_shape(self):
        from afc_ocr.views import key_required_response
        owner, _ = _user("ug_owner")
        org = _org("ug-org", owner)
        r = key_required_response(extract.OcrKeyRequired(org))
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.data["code"], "ocr_key_required")
        self.assertEqual(r.data["organization_slug"], "ug-org")
        self.assertIn("Connect your own AI key", r.data["message"])

    def test_leaderboard_ocr_answers_402_when_the_free_read_is_gone(self):
        from afc_leaderboard.models import StandaloneLeaderboard
        owner, tok = _user("ug_lb_owner")
        org = _org("ug-lb-org", owner)
        OrganizationMember.objects.filter(organization=org, user=owner).update(can_upload_results=True)
        org.ocr_free_reads_left = 0
        org.save(update_fields=["ocr_free_reads_left"])
        lb = StandaloneLeaderboard.objects.create(name="LB", format="team", organization=org, creator=owner)
        from django.core.files.uploadedfile import SimpleUploadedFile
        from afc_organizers.views_ai_key import sample_screenshot
        img = SimpleUploadedFile("shot.png", sample_screenshot(), content_type="image/png")
        with patch("afc_leaderboard.views.validate_ocr_images", return_value=None):
            r = Client().post(f"/leaderboards/standalone/{lb.pk}/ocr/", {"screenshot": img},
                              HTTP_AUTHORIZATION=f"Bearer {tok}")
        self.assertEqual(r.status_code, 402, r.content)
        self.assertEqual(r.json()["code"], "ocr_key_required")
        self.assertEqual(r.json()["organization_slug"], "ug-lb-org")

    def test_batch_run_answers_402_up_front_without_queueing(self):
        from afc_leaderboard.models import LeaderboardOcrJob, StandaloneLeaderboard
        owner, tok = _user("ug_batch_owner")
        org = _org("ug-batch-org", owner)
        OrganizationMember.objects.filter(organization=org, user=owner).update(can_upload_results=True)
        org.ocr_free_reads_left = 0
        org.save(update_fields=["ocr_free_reads_left"])
        lb = StandaloneLeaderboard.objects.create(name="LB", format="team", organization=org, creator=owner)
        job = LeaderboardOcrJob.objects.create(leaderboard=lb, created_by=owner, status="failed", error="old")
        with patch("afc_leaderboard.views.process_leaderboard_ocr_job.delay") as delay:
            r = _post(tok, f"/leaderboards/standalone/{lb.pk}/ocr/jobs/{job.pk}/run/")
            r2 = _post(tok, f"/leaderboards/standalone/{lb.pk}/ocr/run-all/")
        self.assertEqual((r.status_code, r2.status_code), (402, 402), (r.content, r2.content))
        self.assertEqual(r.json()["code"], "ocr_key_required")
        delay.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        # with the free read back, the run is queued
        org.ocr_free_reads_left = 1
        org.save(update_fields=["ocr_free_reads_left"])
        with patch("afc_leaderboard.views.process_leaderboard_ocr_job.delay") as delay:
            r = _post(tok, f"/leaderboards/standalone/{lb.pk}/ocr/jobs/{job.pk}/run/")
        self.assertEqual(r.status_code, 200, r.content)
        delay.assert_called_once()
        org.refresh_from_db()
        self.assertEqual(org.ocr_free_reads_left, 1, "the pre-check never spends the free read")
