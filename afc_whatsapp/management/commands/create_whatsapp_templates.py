# ── afc_whatsapp/management/commands/create_whatsapp_templates.py ─────────────────────────────
# Create AFC's WhatsApp message templates through the Graph API instead of Meta's web form.
#
# WHY THIS EXISTS (owner 2026-08-06). The templates were originally built by hand in WhatsApp
# Manager and ended up on the WRONG WhatsApp Business Account: the WABA that the sending phone
# number belongs to contained only Meta's `hello_world` sample, so every send failed with
# "(#100) Invalid parameter" - the template name was genuinely invalid FOR THAT SENDER. Three
# separate debugging rounds went into finding that, because Meta's error carries no detail.
#
# Doing it through the API fixes the class of problem, not just this instance:
#   * the names come from the SAME settings the sender reads, so a template can never again be
#     approved under a name the code does not ask for (the spec doc had drifted to an `afc_`
#     prefix that the .env never used - exactly the mistake this prevents);
#   * the target WABA is printed and confirmed before anything is created;
#   * re-running is safe, because a name that already exists is reported and skipped.
#
# USAGE
#     python manage.py create_whatsapp_templates                  # DRY RUN: shows what it would do
#     python manage.py create_whatsapp_templates --check          # list what the WABA has today
#     python manage.py create_whatsapp_templates --apply          # actually submit them
#     python manage.py create_whatsapp_templates --apply --only broadcast,room_details
#
# Submitting sends them to Meta for REVIEW. Approval takes minutes to hours; nothing sends until
# a template is APPROVED, and the code already no-ops when a template name is blank.
#
# CONNECTS TO: afc/settings.py (WHATSAPP_* names and the WABA id), afc_whatsapp/client.py (which
# sends against these exact names), afc_auth/broadcast_whatsapp.py (the `broadcast` one), and
# afc_tournament_and_scrims/whatsapp_room_details.py (`room_details` / `room_3d_help`).
import json
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand


# How long to wait after deleting a template before re-creating the same name. Meta's own
# error says "less than 1 minute"; 70 seconds gives that a margin rather than racing it.
_DELETE_SETTLE_SECONDS = 70


def _cfg(name, default=""):
    return getattr(settings, name, default) or default


# The template bodies, verbatim from docs/whatsapp-templates-to-submit.md.
#
# The NAME of each is read from settings rather than written here, because settings is what the
# sending code uses. If the two ever disagree the send fails with an error Meta will not explain,
# which is the whole reason this file exists.
#
# `example.body_text` is REQUIRED by Meta: a submission without sample values is rejected outright.
def _templates():
    return [
        {
            "setting": "WHATSAPP_ROOM_TEMPLATE",
            "lang_setting": "WHATSAPP_ROOM_TEMPLATE_LANG",
            "category": "UTILITY",
            "body": (
                "Hi {{1}}, your squad's match lobby for {{2}} is open.\n\n"
                "Map: {{3}}\n"
                "Room name: {{4}}\n"
                "Room ID: {{5}}\n"
                "Room key: {{6}}\n\n"
                "Open Free Fire, find the custom room by that ID and enter the key to take your "
                "seat. Join before the countdown ends, and message your organizer if you cannot "
                "get in.\n\n"
                "This is a game lobby for a scheduled match. It is not a login and not an "
                "account code."
            ),
            # Meta flagged an earlier draft as AUTHENTICATION because "Password:" above a numeric
            # sample reads like a one-time code. The wording above, and this numeric-but-prefixed
            # sample, are what keep it in UTILITY. Do not "tidy" either.
            "example": ["SYN.HENRYx7", "Legacy Scrims Day 13", "Bermuda",
                        "AFC LOBBY 1", "AFC3D-1234", "284915"],
        },
        # The 3D-room follow-up template was REMOVED from this registry on 2026-08-17 (owner).
        # Meta bills per template message, so sending it doubled the WhatsApp cost of every 3D map
        # in order to repeat joining steps the player already has on the event page, in their
        # in-app notification and in their email. It is no longer registered, no longer submitted
        # for approval and no longer sent. The steps themselves are unchanged and still live in
        # afc_tournament_and_scrims/room_join_help.py.
        {
            "setting": "WHATSAPP_ORDER_RECEIVED_TEMPLATE",
            "lang_setting": "WHATSAPP_ORDER_TEMPLATE_LANG",
            "category": "UTILITY",
            "body": (
                "Thanks {{1}}, we have your order {{2}}.\n\n"
                "What you ordered: {{3}}\n"
                "Total: {{4}}\n\n"
                "We will message you again as soon as it ships. You can reply to this message if "
                "anything looks wrong."
            ),
            "example": ["Ama", "AFC-10482", "520 Diamonds", "NGN 7,500"],
        },
        {
            "setting": "WHATSAPP_ORDER_SHIPPED_TEMPLATE",
            "lang_setting": "WHATSAPP_ORDER_TEMPLATE_LANG",
            "category": "UTILITY",
            "body": (
                "Good news {{1}}, order {{2}} is on its way.\n\n"
                "{{3}}\n\n"
                "We will check in with you once it should have arrived."
            ),
            "example": ["Ama", "AFC-10482",
                        "Sent to your Free Fire account. Allow up to 30 minutes for the diamonds "
                        "to appear."],
        },
        {
            "setting": "WHATSAPP_ORDER_DELIVERED_TEMPLATE",
            "lang_setting": "WHATSAPP_ORDER_TEMPLATE_LANG",
            "category": "UTILITY",
            "body": (
                "Hi {{1}}, your order {{2}} should have arrived by now.\n\n"
                "Did you get it?"
            ),
            "example": ["Ama", "AFC-10482"],
            # A tap comes back as an inbound webhook carrying the payload. afc_whatsapp/webhooks.py
            # reads inbound messages for STOP-style opt-outs today and does NOT yet route a button
            # reply to an order - a small, separate change. Submitting now is still worth it,
            # because approval is the slow part.
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Yes, received"},
                {"type": "QUICK_REPLY", "text": "No, not yet"},
            ],
        },
        {
            # ACCOUNT RECOVERY CODE (owner 2026-08-08). Sent to the WhatsApp number already on an
            # account so somebody locked out of their email can prove the account is theirs. Read
            # by afc_auth.two_factor.WhatsAppCodeMethod, driven by afc_auth/views_recovery.py.
            "setting": "WHATSAPP_LOGIN_CODE_TEMPLATE",
            "lang_setting": "WHATSAPP_LOGIN_CODE_LANG",
            # AUTHENTICATION, and this was SETTLED BY META rather than chosen.
            #
            # First submitted as UTILITY on 2026-08-30, worded about account access rather
            # than as a bare "here is your code", in the hope of staying out of Meta's
            # authentication rules. Meta refused it INSTANTLY, which is an automatic policy
            # match and not a review:
            #
            #     afc_account_recovery_code  en  REJECTED   reason: INCORRECT_CATEGORY
            #
            # A one-time code is authentication content and Meta will not take it as anything
            # else. The guess is over.
            #
            # AN AUTHENTICATION TEMPLATE HAS NO BODY TEXT OF ITS OWN. Meta owns the copy and
            # renders "{{1}} is your verification code." plus an optional security line and an
            # expiry footer. Supplying custom text is refused, which is why `body` and
            # `example` are gone from this entry and `auth` replaces them, and why the careful
            # wording that used to live here is not preserved anywhere: there is nowhere to
            # put it.
            #
            # IT ALSO CHANGES THE SEND. An authentication template REQUIRES a button component
            # carrying the code, on top of the body parameter, and Meta refuses the send
            # without it. See client.send_template's `otp_code`, added in the same change.
            # Nothing else in AFC sends one of these.
            #
            # Billing: authentication is its own price band, separate from utility.
            "category": "AUTHENTICATION",
            # The shape Meta expects. `code_expiration_minutes` is 10 to match the recovery
            # challenge's own lifetime; if that changes, change this and resubmit, or the
            # message promises a window AFC does not honour.
            "auth": {
                "add_security_recommendation": True,
                "code_expiration_minutes": 10,
                "otp_type": "COPY_CODE",
                "button_text": "Copy code",
            },
        },
        {
            "setting": "WHATSAPP_BROADCAST_TEMPLATE",
            "lang_setting": "WHATSAPP_BROADCAST_TEMPLATE_LANG",
            # MARKETING, and billed accordingly: about $0.0516 per message to a Nigerian number
            # against $0.0067 for the UTILITY ones above. That price difference is why sending on
            # this one is restricted to head admins.
            "category": "MARKETING",
            "body": (
                "Hi {{1}}, an update from AFC.\n\n"
                "{{2}}\n\n"
                "Reply STOP to this number if you would rather not get these."
            ),
            "example": ["Layott", "Registration for the August Grand Finals closes tonight."],
        },
    ]


class Command(BaseCommand):
    help = "Create AFC's WhatsApp message templates on the configured WABA via the Graph API."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually submit. Without this the command only shows the plan.")
        parser.add_argument("--check", action="store_true",
                            help="List the templates the WABA already has, and stop.")
        parser.add_argument("--only", default="",
                            help="Comma separated template NAMES to act on (default: all).")
        parser.add_argument("--waba", default="",
                            help="Override the WABA id from settings.")
        parser.add_argument("--delete-rejected", action="store_true",
                            help="Delete REJECTED templates so a corrected version can be "
                                 "submitted under the same name. Meta keeps the name occupied "
                                 "until the rejected row is removed.")

    # ── HTTP ──
    def _graph(self, path, token, method="GET", body=None):
        url = f"https://graph.facebook.com/{_cfg('WHATSAPP_API_VERSION', 'v21.0')}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
        except urllib.error.HTTPError as exc:
            try:
                return None, json.loads(exc.read())
            except Exception:  # noqa: BLE001 - a non-JSON error body must not mask the failure
                return None, {"error": {"message": f"HTTP {exc.code}"}}
        except Exception as exc:  # noqa: BLE001
            return None, {"error": {"message": str(exc)}}

    def handle(self, *args, **opts):
        token = _cfg("WHATSAPP_TOKEN") or _cfg("WHATSAPP_ACCESS_TOKEN")
        waba = opts["waba"] or _cfg("WHATSAPP_BUSINESS_ACCOUNT_ID")
        phone_id = _cfg("WHATSAPP_PHONE_NUMBER_ID")

        if not token or not waba:
            self.stderr.write("WHATSAPP_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID must both be set.")
            return

        self.stdout.write(f"WABA        : {waba}")
        self.stdout.write(f"phone id    : {phone_id}")

        # THE CHECK THAT WOULD HAVE SAVED THREE ROUNDS OF DEBUGGING: the templates must live on the
        # SAME WABA as the number sending them. Printing both together makes a mismatch obvious.
        numbers, err = self._graph(f"{waba}/phone_numbers?fields=id,display_phone_number", token)
        if err:
            self.stdout.write(self.style.WARNING(f"could not list numbers: {err}"))
        else:
            ids = [n.get("id") for n in numbers.get("data", [])]
            for n in numbers.get("data", []):
                self.stdout.write(f"  number on this WABA: {n.get('display_phone_number')} ({n.get('id')})")
            if phone_id and phone_id not in ids:
                self.stdout.write(self.style.ERROR(
                    f"\n  STOP. The sending number {phone_id} is NOT on this WABA.\n"
                    f"  Creating templates here would repeat the original mistake: they would be\n"
                    f"  approved on an account the sender cannot use. Fix\n"
                    f"  WHATSAPP_BUSINESS_ACCOUNT_ID (or WHATSAPP_PHONE_NUMBER_ID) first."))
                return

        existing, err = self._graph(
            f"{waba}/message_templates?limit=100"
            f"&fields=name,language,status,category,rejected_reason,quality_score,"
            f"components", token)
        if err:
            self.stderr.write(f"could not read templates: {json.dumps(err)}")
            return
        have = {(t["name"], t["language"]): t for t in existing.get("data", [])}
        self.stdout.write(f"\nalready on this WABA ({len(have)}):")
        for (name, lang), t in sorted(have.items()):
            self.stdout.write(f"  {name:26} {lang:6} {t['status']:10} {t.get('category','')}")
            # The REASON is the only useful part of a rejection, and it is not shown anywhere in
            # the submit response - only on a later read. Print it here or the operator is left
            # guessing exactly as we were.
            if t.get("status") == "REJECTED" and t.get("rejected_reason"):
                self.stdout.write(self.style.ERROR(
                    f"      rejected: {t['rejected_reason']}"))
            # The COMPONENTS, for authentication templates only. Added 2026-08-30 after a
            # send failed with Meta 132018 ("there is an issue with the parameters in your
            # template"): a wrong button PARAMETER and a template that has no button at all
            # produce the same error, and neither is visible from name/status/category. So
            # print what Meta actually stored and stop guessing.
            if (t.get("category") or "").upper() == "AUTHENTICATION":
                for comp in t.get("components") or []:
                    kind = comp.get("type")
                    if kind == "BUTTONS":
                        for b in comp.get("buttons") or []:
                            self.stdout.write(
                                f"      button: type={b.get('type')} "
                                f"otp_type={b.get('otp_type')} text={b.get('text')!r}")
                    else:
                        detail = {k: v for k, v in comp.items() if k != "type"}
                        self.stdout.write(f"      {kind.lower()}: {json.dumps(detail)[:120]}")

        # Clearing a rejected row frees its NAME. Meta will not accept a second template under a
        # name that already exists, even a rejected one, so a corrected body cannot be submitted
        # until the old row is gone.
        deleted = []
        if opts["delete_rejected"]:
            for (name, lang), t in sorted(have.items()):
                if t.get("status") != "REJECTED":
                    continue
                _res, derr = self._graph(
                    f"{waba}/message_templates?name={name}", token, method="DELETE")
                if derr:
                    self.stdout.write(self.style.ERROR(f"  delete {name}: {json.dumps(derr)[:160]}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"  deleted rejected template: {name}"))
                    have.pop((name, lang), None)
                    deleted.append(name)

        if opts["check"]:
            return

        # META'S DELETE IS ASYNCHRONOUS, and creating the same name under a DIFFERENT
        # category while the old content is still going away is refused:
        #
        #   You can't change the category for this message template while the existing
        #   English content is being deleted. Try again in less than 1 minute or use
        #   UTILITY as the category.
        #
        # Hit on 2026-08-30 taking the account-recovery template from UTILITY to
        # AUTHENTICATION: `--delete-rejected --apply` deleted and re-created in one breath
        # and Meta refused the create. Waiting here turns a two-run dance into one command
        # that works. Only when a delete ACTUALLY happened and we are about to create, so
        # the ordinary run costs nothing.
        if deleted and opts["apply"]:
            self.stdout.write("")
            self.stdout.write(
                f"waiting {_DELETE_SETTLE_SECONDS}s for Meta to finish deleting "
                f"{', '.join(deleted)} before re-creating it."
            )
            self.stdout.write(
                "Meta refuses a category change while the old content is still being "
                "removed, and its own advice is to try again in under a minute."
            )
            time.sleep(_DELETE_SETTLE_SECONDS)

        only = {s.strip() for s in opts["only"].split(",") if s.strip()}
        planned = []
        for spec in _templates():
            name = _cfg(spec["setting"])
            lang = _cfg(spec["lang_setting"], "en")
            if not name:
                self.stdout.write(self.style.WARNING(
                    f"  skip: {spec['setting']} is not set in the environment"))
                continue
            if only and name not in only:
                continue
            planned.append((name, lang, spec))

        self.stdout.write("\nplan:")
        for name, lang, spec in planned:
            state = "EXISTS, skip" if (name, lang) in have else "CREATE"
            self.stdout.write(f"  {name:26} {lang:6} {spec['category']:10} -> {state}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN. Re-run with --apply to submit."))
            return

        self.stdout.write("")
        for name, lang, spec in planned:
            if (name, lang) in have:
                self.stdout.write(f"  {name}: already exists, skipped")
                continue
            auth = spec.get("auth")
            if auth:
                # An AUTHENTICATION template is built from OPTIONS, never from text: Meta owns
                # the copy and refuses a body of our own. The OTP button is mandatory rather
                # than a nicety, and it is what lets the recipient copy the code without
                # retyping it off a notification.
                components = [
                    {"type": "BODY",
                     "add_security_recommendation": auth["add_security_recommendation"]},
                    {"type": "FOOTER",
                     "code_expiration_minutes": auth["code_expiration_minutes"]},
                    {"type": "BUTTONS", "buttons": [{
                        "type": "OTP",
                        "otp_type": auth["otp_type"],
                        "text": auth["button_text"],
                    }]},
                ]
            else:
                components = [{
                    "type": "BODY",
                    "text": spec["body"],
                    "example": {"body_text": [spec["example"]]},
                }]
                if spec.get("buttons"):
                    components.append({"type": "BUTTONS", "buttons": spec["buttons"]})

            created, err = self._graph(
                f"{waba}/message_templates", token, method="POST",
                body={"name": name, "language": lang, "category": spec["category"],
                      "components": components})
            if err:
                msg = (err.get("error") or {}).get("error_user_msg") \
                    or (err.get("error") or {}).get("message") or json.dumps(err)
                self.stdout.write(self.style.ERROR(f"  {name}: FAILED - {msg}"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  {name}: submitted (id {created.get('id')}, status {created.get('status')})"))

        self.stdout.write(
            "\nMeta reviews each one. Nothing sends until a template is APPROVED; re-run with "
            "--check to watch the statuses.")
