# ──────────────────────────────────────────────────────────────────────────────
# whatsapp_recovery_doctor - why did a WhatsApp recovery code not arrive?
#
# WHY THIS EXISTS (owner, 2026-08-30: "the recover with whatsapp does not work",
# then "says sent, nothing arrives")
#
#     /recover-account is deliberately opaque. Step 1 answers an unknown identifier,
#     an account with no number, an account whose number is over a year old, an
#     account that opted out of WhatsApp, AND a real account whose send failed with
#     the SAME message, the SAME status and the same response shape, right down to a
#     recovery_token backed by nothing. That is correct: anything else lets a stranger
#     ask the endpoint which accounts exist and which hold a phone number.
#
#     The cost is that "it does not work" is FIVE different faults wearing one face,
#     and nobody, including an admin, could tell them apart. There was no way to ask
#     the question, so this is that way. It changes nothing and sends nothing: it
#     walks the same checks afc_auth/views_recovery.py walks, in the same order, and
#     prints which one an account actually hits.
#
# READ ONLY, and that is load bearing. It mints no challenge, writes no row and calls
# no Meta endpoint. Running it against a real player's account must not consume their
# one grant, must not burn an attempt, and must not put a code on their phone.
#
# USE
#     python manage.py whatsapp_recovery_doctor <username, email or phone>
#     python manage.py whatsapp_recovery_doctor --config-only
#
# WHAT IT CANNOT TELL YOU: whether Meta actually put the message on the handset. The
# last section prints the delivery receipts AFC received (sent, delivered, read) and
# any error Meta returned, which is everything this side of the wire knows.
#
# CONNECTS TO
#     afc_auth/views_recovery.py        the flow being diagnosed; the check ORDER here
#                                       deliberately mirrors recovery_start
#     afc_auth/two_factor.py            WhatsAppCodeMethod, which resolves the number
#     afc_whatsapp/tasks.py             is_send_allowed, the registry gate
#     afc_whatsapp/models.py            WhatsAppTemplate, WhatsAppMessage
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

OK = "  ok   "
BAD = "  FAIL "
WARN = "  warn "
INFO = "       "


class Command(BaseCommand):
    help = "Explain why a WhatsApp account-recovery code did not arrive. Sends nothing."

    def add_arguments(self, parser):
        parser.add_argument(
            "identifier",
            nargs="?",
            help="username, email or phone of the account that could not recover",
        )
        parser.add_argument(
            "--config-only",
            action="store_true",
            help="check the server's WhatsApp configuration and stop, naming no account",
        )
        parser.add_argument(
            "--messages",
            type=int,
            default=5,
            help="how many recent WhatsApp sends to that account to list (default 5)",
        )

    # ── §1 the server, which is the half that fails for EVERYONE at once ──────
    def _config(self):
        """Settings and registry state. Nothing here names an account, so it is the
        part worth checking first: if this section is red, the account section cannot
        explain anything."""
        from afc_whatsapp.models import WhatsAppTemplate

        self.stdout.write(self.style.MIGRATE_HEADING("\n1. Server configuration"))

        template = (getattr(settings, "WHATSAPP_LOGIN_CODE_TEMPLATE", "") or "").strip()
        language = (getattr(settings, "WHATSAPP_LOGIN_CODE_LANG", "") or "en").strip()

        if not template:
            # WhatsAppCodeMethod.deliver treats blank as an off switch and returns False
            # BEFORE calling anything, so no row is written and no error is raised. From
            # the outside this is indistinguishable from a successful send.
            self.stdout.write(
                BAD + "WHATSAPP_LOGIN_CODE_TEMPLATE is not set. Nothing is ever sent."
            )
            self.stdout.write(
                INFO + "     A blank template name is the documented off switch, so the "
                "code is not\n"
                + INFO + "     even attempted and the page still says a code was sent."
            )
        else:
            self.stdout.write(OK + f"WHATSAPP_LOGIN_CODE_TEMPLATE = {template!r}")
        self.stdout.write(INFO + f"WHATSAPP_LOGIN_CODE_LANG     = {language!r}")

        # Credentials are reported as present or absent, NEVER printed. A support
        # transcript with an access token in it is a leak that outlives the incident.
        for name in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN"):
            present = bool((getattr(settings, name, "") or "").strip())
            self.stdout.write((OK if present else BAD) + f"{name} is "
                              + ("set" if present else "NOT SET, so no send can be made"))

        total = WhatsAppTemplate.objects.count()
        if total == 0:
            # is_send_allowed lets everything through when the registry is empty, on the
            # grounds that a missing ops step must not block every message. So this is a
            # warning, not a failure: Meta becomes the judge instead of us.
            self.stdout.write(
                WARN + "The template registry is EMPTY. `manage.py sync_whatsapp_templates` "
                "has never run here,"
            )
            self.stdout.write(
                INFO + "     so sends are attempted blind and Meta decides. Run it to see "
                "what it actually granted."
            )
        else:
            self.stdout.write(INFO + f"Template registry holds {total} row(s).")

        if template:
            rows = list(WhatsAppTemplate.objects.filter(name=template).order_by("language"))
            if not rows:
                self.stdout.write(
                    (WARN if total == 0 else BAD)
                    + f"No registry row for {template!r} in ANY language."
                )
                if total:
                    self.stdout.write(
                        INFO + "     The registry is non-empty, so is_send_allowed REFUSES "
                        "this template locally.\n"
                        + INFO + "     Nothing reaches Meta. Run sync_whatsapp_templates."
                    )
            for row in rows:
                state = "approved" if row.approved else "NOT APPROVED"
                mark = OK if row.approved else BAD
                self.stdout.write(
                    mark + f"{row.name} [{row.language}] {state}, category "
                    f"{row.category or 'unknown'}, {row.variable_count} variable(s)"
                )
                # THE KNOWN GAP, called out by name because it is the one failure that
                # looks like a healthy configuration. See afc_whatsapp/client.py
                # send_template: it builds body, quick_reply and url components, and no
                # copy_code / one-tap OTP button. Meta REQUIRES that component on an
                # AUTHENTICATION template and rejects the send without it.
                if (row.category or "").upper() == "AUTHENTICATION":
                    self.stdout.write(
                        BAD + "  ^ AUTHENTICATION category. client.send_template does NOT "
                        "build the OTP button"
                    )
                    self.stdout.write(
                        INFO + "    component that category requires, so Meta rejects every "
                        "send of it. This is\n"
                        + INFO + "    a known, documented gap (see the comment in "
                        "create_whatsapp_templates.py)."
                    )
            if rows and not any(r.language == language for r in rows):
                # Meta treats "en" and "en_US" as different templates and answers a
                # mismatch with 132001.
                self.stdout.write(
                    BAD + f"No row for the configured language {language!r}. Meta treats "
                    f"'en' and 'en_US'"
                )
                self.stdout.write(
                    INFO + "     as different templates and refuses a mismatch with error "
                    "132001."
                )
        return template, language

    # ── §2 the account, in recovery_start's own order ─────────────────────────
    def _account(self, identifier):
        """Walk the branches recovery_start walks, and name the one that fires.

        Order matters and mirrors the view: an account can fail more than one of these
        and only the first one reached is the reason.
        """
        from afc_auth import two_factor
        from afc_auth.models import canonical_profile
        from afc_auth.views_recovery import RECOVERY_NUMBER_MAX_AGE, _number_too_stale
        # The SAME resolver recovery_start uses, so "the account I log into" and "the
        # account this walks" cannot drift apart.
        from afc_auth.identifiers import resolve_login_identifier
        from afc_whatsapp.phone import mask_e164, to_e164

        self.stdout.write(self.style.MIGRATE_HEADING("\n2. The account"))

        user = resolve_login_identifier(identifier)
        if user is None:
            self.stdout.write(BAD + f"No account resolves from {identifier!r}.")
            self.stdout.write(
                INFO + "     recovery_start answers this with the same body as success, "
                "so the page still\n"
                + INFO + "     says a code was sent. Check for a typo, or a changed "
                "username."
            )
            return None
        self.stdout.write(OK + f"Resolved to {user.username!r} (id {user.pk}).")

        # canonical_profile, NOT profile_of: duplicate UserProfile rows exist in
        # production and this is the resolver every reader and writer agrees on.
        profile = canonical_profile(user)
        if profile is None:
            self.stdout.write(BAD + "The account has NO UserProfile row, so no number.")
            return user
        self.stdout.write(INFO + f"Canonical profile id {profile.pk}.")

        raw = (getattr(profile, "whatsapp_number", "") or "").strip()
        if not raw:
            self.stdout.write(BAD + "No WhatsApp number saved. Nothing can be sent.")
            self.stdout.write(
                INFO + "     Only about 1.7% of accounts have one, so this is the single "
                "most likely answer."
            )
            return user

        country = getattr(user, "ip_country", "") or getattr(user, "country", "") or None
        e164 = to_e164(raw, country) or ""
        if not e164:
            self.stdout.write(
                BAD + f"The saved number does not normalise to E.164 (country {country!r})."
            )
            self.stdout.write(
                INFO + "     Stored values predate the country-code rule, so some are in "
                "local form."
            )
            return user
        # MASKED. This command gets run and pasted into chat; a full number does not
        # need to travel with it.
        self.stdout.write(OK + f"Number normalises to {mask_e164(e164)}.")

        if not getattr(profile, "whatsapp_opt_in", True):
            self.stdout.write(BAD + "The account has WhatsApp switched OFF (opted out).")
            self.stdout.write(
                INFO + "     Opting out also switches off recovery, which the profile "
                "settings copy says.\n"
                + INFO + "     Only the account holder can turn it back on."
            )
            return user
        self.stdout.write(OK + "WhatsApp is opted in.")

        stamped = getattr(profile, "whatsapp_number_updated_at", None)
        if stamped is None:
            self.stdout.write(WARN + "whatsapp_number_updated_at is empty.")
        else:
            age = timezone.now() - stamped
            if _number_too_stale(user):
                self.stdout.write(
                    BAD + f"The number was last confirmed {age.days} days ago, over the "
                    f"{RECOVERY_NUMBER_MAX_AGE.days} day limit."
                )
                self.stdout.write(
                    INFO + "     Recovery refuses it because a given-up line gets reissued. "
                    "The only way back\n"
                    + INFO + "     is the account holder re-saving the number in profile "
                    "settings, which restarts\n"
                    + INFO + "     the clock even if the digits do not change."
                )
                return user
            self.stdout.write(OK + f"Number last confirmed {age.days} days ago, inside the "
                              f"{RECOVERY_NUMBER_MAX_AGE.days} day limit.")

        # The method's own answer, which is what issue_challenge consults. If everything
        # above passed and this still says no, the disagreement is itself the finding.
        method = two_factor.METHODS["whatsapp"]
        available = method.is_available(user)
        self.stdout.write(
            (OK if available else BAD)
            + f"WhatsAppCodeMethod.is_available -> {available}"
        )
        if not available:
            self.stdout.write(
                INFO + "     issue_challenge would answer 'unavailable' and mint nothing."
            )
        return user

    # ── §3 what Meta said, which is the only evidence about the wire ──────────
    def _messages(self, user, limit):
        from afc_whatsapp.models import WhatsAppMessage

        self.stdout.write(self.style.MIGRATE_HEADING("\n3. Recent WhatsApp sends to this account"))
        rows = list(
            WhatsAppMessage.objects.filter(user=user, direction="outbound")
            .order_by("-id")[:limit]
        )
        if not rows:
            self.stdout.write(
                WARN + "No outbound WhatsApp row for this account, ever."
            )
            self.stdout.write(
                INFO + "     A row is written BEFORE Meta is called, so no row means the "
                "send was never\n"
                + INFO + "     attempted: an off switch or a local refusal, not a Meta "
                "problem. On production\n"
                + INFO + "     the row is written by the worker, so also check that a "
                "Celery worker is\n"
                + INFO + "     consuming the whatsapp queue."
            )
            return

        for row in rows:
            when = timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M")
            line = (f"{when}  {row.status:<9} {row.template_name or '(text)'} "
                    f"[{row.template_language or '-'}]  {row.context or ''}")
            if row.error_code:
                self.stdout.write(BAD + line)
                self.stdout.write(
                    INFO + f"     Meta error {row.error_code}: {row.error_title}"
                )
                if row.error_code == 132001:
                    self.stdout.write(
                        INFO + "     132001 = template name or LANGUAGE does not match an "
                        "approved template."
                    )
                elif row.error_code in (132000, 132005, 132012, 132015):
                    self.stdout.write(
                        INFO + "     A 1320xx is a template SHAPE complaint: wrong number of "
                        "parameters, or a\n"
                        + INFO + "     required component missing. An AUTHENTICATION template "
                        "needs an OTP button\n"
                        + INFO + "     that client.send_template does not build. See section 1."
                    )
            elif row.status == "queued":
                self.stdout.write(
                    WARN + line + "   (never left the queue: check the Celery worker)"
                )
            else:
                self.stdout.write(OK + line)

        recovery = [r for r in rows if r.context == "account_recovery_code"]
        if not recovery:
            self.stdout.write(
                WARN + "None of the above is a recovery code (context "
                "'account_recovery_code')."
            )

    def handle(self, *args, **options):
        self.stdout.write(
            "WhatsApp recovery doctor. Read only: this sends nothing and changes nothing."
        )
        template, _language = self._config()

        if options["config_only"]:
            return
        identifier = options["identifier"]
        if not identifier:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo identifier given, so only the configuration was checked. Pass a "
                    "username, email or phone to walk one account, or --config-only to say "
                    "you meant this."
                )
            )
            return

        user = self._account(identifier)
        if user is not None:
            self._messages(user, options["messages"])

        self.stdout.write(self.style.MIGRATE_HEADING("\nReading this"))
        self.stdout.write(
            "The FIRST failing line is the reason. recovery_start returns the same body for\n"
            "every one of them, so the page saying a code was sent tells you nothing either\n"
            "way. A clean run with no row in section 3 means the send was never attempted."
        )
