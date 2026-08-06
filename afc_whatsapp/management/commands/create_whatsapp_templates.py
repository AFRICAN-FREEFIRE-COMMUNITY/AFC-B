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
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand


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
        {
            "setting": "WHATSAPP_ROOM_3D_TEMPLATE",
            "lang_setting": "WHATSAPP_ROOM_TEMPLATE_LANG",
            "category": "UTILITY",
            # "Hi " in front is NOT cosmetic. Meta rejects a body that STARTS or ENDS with a
            # variable ("Variables can't be at the start or end of the template"), and the doc's
            # wording opened on {{1}}. Caught on the first real submission, 2026-08-06.
            "body": (
                "Hi {{1}}, one more thing about your room for {{2}}: it is a 3D room, so joining "
                "works differently.\n\n"
                "Create a group and add all 4 squad members. The group leader then goes to "
                "Customs, then League, or searches the room ID. Tap the join icon, then enter "
                "your team name, team tag and password.\n\n"
                "Use the account registered on the AFC website. If you do not, your results will "
                "not count and your team could be penalized."
            ),
            "example": ["SYN.HENRYx7", "Legacy Scrims Day 13"],
        },
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
            f"&fields=name,language,status,category,rejected_reason,quality_score", token)
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

        # Clearing a rejected row frees its NAME. Meta will not accept a second template under a
        # name that already exists, even a rejected one, so a corrected body cannot be submitted
        # until the old row is gone.
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

        if opts["check"]:
            return

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
