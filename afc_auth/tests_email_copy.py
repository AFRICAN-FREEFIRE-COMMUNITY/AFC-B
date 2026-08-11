"""
afc_auth.tests_email_copy - guards on the hand-authored transactional-email catalog.

WHY THIS FILE EXISTS (owner backlog #18, 2026-08-05)
    afc_auth/email_i18n.py holds the finished sentences of every fixed transactional email in
    three languages. Nothing about it is type-checked and nothing about it is exercised by the
    normal view tests, because the builders that render it are only reached on a real send. The
    result is a class of bug that is invisible in code review and only shows up in one language,
    in a stranger's inbox:

      1. A translated sentence quietly drops a {placeholder}. The French recipient gets
         "Votre inscription est confirmée." with no event name in it, and nobody notices because
         the English one is fine.
      2. A rewritten sentence gains a {placeholder} the BUILDER never passes. Most builders call
         .format() inside an f-string with no try/except (afc_auth/views.py, afc_shop/emails.py,
         afc_player_market/views.py, afc_tournament_and_scrims/views.py), so this raises KeyError
         and the email is never sent at all.
      3. A key exists in English but not in French, so copy_for()[key] raises for one locale only.
      4. An em dash or en dash gets into user-facing copy, which the owner has banned outright.

    Every test below is one of those four failure modes. They are cheap, they need no database,
    and they are the only thing standing between a copy edit and a broken sentence in production.

HOW IT CONNECTS
    Reads afc_auth.email_i18n SUBJECTS and COPY directly. CALLER_PLACEHOLDERS below mirrors, key by
    key, what each rendering call site actually passes to .format(); it is the executable version
    of the "COPY RULES" comment at the top of email_i18n.py. When you add a template, add its row
    here too, or test_every_copy_key_is_declared will fail and tell you.

Run: .venv/Scripts/python.exe manage.py test afc_auth.tests_email_copy
"""
import re
import string

from django.test import SimpleTestCase

from afc_auth.email_i18n import COPY, SUBJECTS, copy_for, subject_for

LANGS = ("en", "fr", "pt")


def placeholders(text):
    """The set of {named} fields in a str.format() template.

    string.Formatter().parse yields (literal, field_name, format_spec, conversion) per chunk;
    field_name is None on the trailing literal, which is why the None is filtered out. Positional
    "{}" fields would come back as "" and are treated as a name so the tests below flag them: no
    sentence in this catalog uses positional formatting.
    """
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# What each call site actually passes to .format(), per template, per sentence key.
#
# Sourced by reading every render site once:
#   afc_auth/views.py                    email_verification_code / _welcome / _reset_token /
#                                        _password_changed / _change_code / _email_changed
#   afc_shop/emails.py                   _summary_table + the three order builders
#   afc_shop/fulfilment.py               notify_vendor
#   afc_sponsors/engagements.py          _notify_rejection
#   afc_tournament_and_scrims/views.py   check_and_activate_team / confirm_player / reject_player
#   afc_player_market/views.py           the five player-market builders
#   afc_partner_apply/emails.py          _send (passes the whole **fmt bag to every listed key)
#
# A sentence may use FEWER placeholders than its caller offers (str.format ignores extra kwargs).
# It may never use MORE: that is the KeyError that stops the email going out entirely.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
CALLER_PLACEHOLDERS = {
    "verification_code": {
        "heading": set(), "intro": {"username", "site"}, "expires": set(), "disclaimer": set(),
    },
    "welcome": {
        "heading": {"username"}, "intro": set(), "cta": set(),
        "feat1": set(), "feat2": set(), "feat3": set(),
    },
    "reset_token": {
        "heading": set(), "intro": set(), "expires": set(), "disclaimer": set(),
    },
    "password_changed": {
        "heading": set(), "intro": {"username", "when"}, "warning": {"support"},
        "support_label": set(),
    },
    "change_code": {
        "heading": set(), "intro": set(), "expires": set(), "disclaimer": set(),
    },
    # afc_auth/views.py email_two_factor_code, called by the EmailCodeMethod in two_factor.py.
    # No placeholders at all: the code itself is rendered by _email_code() as its own block, never
    # interpolated into a sentence, so there is nothing for str.format to fill.
    "two_factor_code": {
        "heading": set(), "intro": set(), "expires": set(), "disclaimer": set(),
    },
    "email_changed": {
        "heading": set(), "intro": {"username", "new_email", "when"}, "warning": {"support"},
        "support_label": set(),
    },
    # Sent BY SUPPORT to the old AND the new address (afc_auth/views.py
    # email_admin_email_changed, called from views_admin_identity.admin_set_user_email). Same
    # three placeholders as email_changed above, plus two sentences the self-serve version has no
    # need for: `signed_out` (every session was ended) and `two_factor` (only rendered when 2FA
    # actually had to come down). Neither takes a placeholder, so both are rendered raw.
    "admin_email_changed": {
        "heading": set(), "intro": {"username", "new_email", "when"}, "signed_out": set(),
        "warning": {"support"}, "two_factor": set(), "support_label": set(),
    },
    # In-game name changed BY SUPPORT (afc_auth/views.py email_admin_username_changed, called from
    # views_admin_identity.admin_set_user_username). No "signed_out": a name change does not end
    # sessions. "event" is rendered only when the player was mid-event.
    "admin_username_changed": {
        "heading": set(), "intro": {"new_name", "when"}, "signin": set(), "event": set(),
        "warning": {"support"}, "support_label": set(),
    },
    # WhatsApp number changed BY SUPPORT (afc_auth/views.py email_admin_whatsapp_changed, called
    # from views_admin_identity.admin_set_user_whatsapp). "removed" replaces "intro" when the
    # number was cleared, so it carries {when} but NOT {masked_number}.
    "admin_whatsapp_changed": {
        "heading": set(), "intro": {"masked_number", "when"}, "removed": {"when"}, "why": set(),
        "warning": {"support"}, "support_label": set(),
    },
    # Sent to the account's address after a password was reset through WHATSAPP RECOVERY
    # (afc_auth/views.py email_recovery_password_reset, called from
    # views_recovery.recovery_reset_password). Same placeholder set as admin_email_changed MINUS
    # `new_email` (no address changes) and MINUS the `two_factor` sentence, and that second absence
    # is the point: the recovery flow never switches 2FA off and never steps around it, so there is
    # nothing to tell the reader to turn back on. If a `two_factor` key ever appears in that copy,
    # this row should fail first, because its presence would mean the flow started taking the
    # factor down.
    "recovery_password_reset": {
        "heading": set(), "intro": {"username", "when"}, "signed_out": set(),
        "warning": {"support"}, "support_label": set(),
    },
    # Sent to BOTH the old and the new address after the account email was moved through WHATSAPP
    # RECOVERY (afc_auth/views.py email_recovery_email_changed, called from
    # views_recovery.recovery_confirm_email_change). Same shape as admin_email_changed, including
    # `new_email`, MINUS the `two_factor` sentence - and that absence is load-bearing in the same
    # way it is for recovery_password_reset above, but for a different reason: this flow REFUSES
    # outright to run on an account with two-step sign-in on (views_recovery.py §4), so nothing was
    # ever taken down. A `two_factor` key appearing here would mean the refusal had been softened
    # into a tear-down, and this row should fail before anybody notices in production.
    "recovery_email_changed": {
        "heading": set(), "intro": {"username", "new_email", "when"}, "signed_out": set(),
        "warning": {"support"}, "support_label": set(),
    },
    "order_received": {
        "heading": set(), "intro": {"buyer"}, "track": {"link"},
    },
    "order_shipped": {
        "heading": set(), "intro": {"buyer"}, "ship_label": set(), "questions": {"link"},
    },
    "order_completed": {
        "heading": set(), "intro": {"buyer"}, "shop_again": {"link"},
    },
    "order_summary": {
        "order_no": {"id"}, "subtotal": set(), "discount": set(), "tax": set(),
        "total": set(), "delivery_to": set(),
    },
    "vendor_new_order": {
        "heading": set(), "intro": {"order_no", "buyer", "link"},
    },
    # engagements.py renders only "body"; "title" mirrors the SUBJECTS row of the same key and is
    # allowed the same values the subject is built with.
    "sponsor_reject_final": {
        "title": {"sponsor", "label", "event_name", "reason"},
        "body": {"sponsor", "label", "event_name", "reason"},
    },
    "sponsor_reject_retry": {
        "title": {"sponsor", "label", "event_name", "reason"},
        "body": {"sponsor", "label", "event_name", "reason"},
    },
    "team_registered": {
        "congrats": set(), "dear": {"leader", "team_name"}, "verified": set(),
        "box": {"team_name", "event_name"}, "match_details": set(), "stay": set(),
        "need_help": {"email"}, "look_forward": set(), "regards": set(), "board": set(),
        "visit_website": set(), "join_discord": set(),
    },
    "player_accepted": {
        "heading": set(), "dear": {"player"}, "accepted": {"event_name", "status"},
        "status_word": set(), "eligible": set(), "questions": {"email"}, "good_luck": set(),
        "regards": set(), "board": set(),
    },
    "player_accepted_owner": {
        "heading": set(), "dear": {"leader", "team_name"}, "reviewed": {"player", "event_name"},
        "status_label": set(), "status_word": set(), "track": set(), "need_help": {"contact"},
        "contact_support": set(), "thanks": set(), "regards": set(), "board": set(),
    },
    "player_rejected": {
        "heading": set(), "dear": {"player"}, "rejected": {"event_name", "status"},
        "status_word": set(), "reason_label": set(), "correct": set(), "update_btn": set(),
        "need_help": {"contact"}, "contact_support": set(), "regards": set(), "board": set(),
    },
    "player_rejected_owner": {
        "heading": set(), "dear": {"leader", "team_name"}, "reviewed": {"player", "event_name"},
        "status_label": set(), "status_word": set(), "reason_label": set(), "monitor": set(),
        "need_help": {"contact"}, "contact_support": set(), "regards": set(), "board": set(),
    },
    "pm_application_received": {
        "header": set(), "mgmt": {"team"}, "hi": {"mgmt", "team"}, "total_label": set(),
        "applied_sub": set(), "message": set(), "cta": set(), "footer_staff": {"team"},
        "rights": set(),
    },
    "pm_application_rejected": {
        "header": set(), "hi": {"player"}, "body": {"team"}, "reason_label": set(),
        "keep_going_title": set(), "keep_going_body": set(), "cta": set(),
        # footer is rendered WITHOUT .format() (afc_player_market/views.py), so it must stay bare.
        "footer": set(), "rights": set(),
    },
    "pm_trial_started_player": {
        "header": set(), "hey": {"player", "team"}, "team_label": set(),
        "whatnext_title": set(), "whatnext_body": set(), "cta": set(), "footer": {"team"},
        "rights": set(),
    },
    "pm_trial_started_team": {
        "header": set(), "mgmt": {"team"}, "hi": {"mgmt"}, "body": {"player"},
        "player_label": set(), "cta": set(), "footer_staff": {"team"}, "rights": set(),
    },
    "pm_trial_invite": {
        "header": set(), "hey": {"player", "team"}, "team_inviting": set(),
        "message_label": set(), "window_title": set(), "window_body": {"hours"},
        "hours_text": set(), "cta": set(), "footer": set(), "rights": set(),
    },
    "pm_trial_accepted_team": {
        "header": set(), "mgmt": {"team"}, "hi": {"mgmt"}, "body": {"player"},
        "player_label": set(), "cta": set(), "footer_staff": {"team"}, "rights": set(),
    },
    # afc_partner_apply/emails.py _send passes the SAME **fmt bag to every key it renders, so the
    # allowance here is per template rather than per sentence. "heading" is rendered unformatted
    # but is covered by the same bag, which is harmless.
    "partner_apply_received": dict.fromkeys(
        ("heading", "intro", "next_steps", "what_it_is", "guide", "keep_link"),
        {"organisation", "reference", "link", "guide"},
    ),
    "partner_apply_changes": dict.fromkeys(
        ("heading", "intro", "note", "how_to_fix"),
        {"organisation", "reference", "note", "link"},
    ),
    "partner_apply_approved": dict.fromkeys(
        ("heading", "intro", "credentials", "expiry", "guide"),
        {"organisation", "reference", "hours", "claim_link", "link"},
    ),
    "partner_apply_rejected": dict.fromkeys(
        ("heading", "intro", "note", "reapply"),
        {"organisation", "reference", "note"},
    ),
    # Rendered by afc_tournament_and_scrims.event_invite_delivery._invitation_email_html. The three
    # urgency_* keys are alternatives, not a sequence: the campaign's kind picks exactly one, and
    # none of them takes a placeholder because the sentence has to stand on its own in a list of
    # other sentences. The organizer's free-text note is NOT a placeholder here; it is escaped and
    # quoted as its own block, so it can never collide with a brace in the catalog.
    "event_team_invitation": dict.fromkeys(
        ("heading", "intro", "from_organizer", "urgency_per_team", "urgency_fcfs", "urgency_bulk",
         "how_to_answer", "cta"),
        {"team", "event", "name"},
    ),
}

# What each subject_for() call site passes. Mirrors the grep of every subject_for( in the backend.
CALLER_SUBJECT_PLACEHOLDERS = {
    "verify_account": set(),
    "resend_code": set(),
    "welcome": set(),
    "reset_password": set(),
    "resend_reset": set(),
    "password_changed": set(),
    "confirm_new_email": set(),
    "email_changed": set(),
    "email_updated_admin": set(),
    # Both name the FIELD support changed, never a value: these go to an inbox whose owner may not
    # have asked for any of it, and the subject has to be judgeable on its own.
    "username_updated_admin": set(),
    "whatsapp_updated_admin": set(),
    # No placeholders: the subject names the CHANNEL that was used ("using WhatsApp") rather than
    # any value, because this is the tripwire and the reader has to be able to judge it from the
    # subject line alone.
    "password_reset_recovery": set(),
    # Same rule as password_reset_recovery: names the CHANNEL, never a value. This one goes to the
    # OLD address too, where its reader may not have asked for any of it, so the subject has to be
    # judgeable on its own.
    "email_changed_recovery": set(),
    # The code sent to the NEW address during recovery. Distinct from "confirm_new_email" (the
    # signed-in flow) because its reader is locked out and may not remember starting this.
    "confirm_new_email_recovery": set(),
    "two_factor": set(),
    "order_received": set(),
    "order_shipped": set(),
    "order_completed": set(),
    "vendor_new_order": {"order_no"},
    "sponsor_reject_final": {"label", "event_name"},
    "sponsor_reject_retry": {"label", "event_name"},
    "team_registered": {"team_name", "event_name"},
    "player_accepted": {"event_name"},
    "player_accepted_owner": {"player", "event_name"},
    "player_rejected": {"event_name"},
    "player_rejected_owner": {"player", "event_name"},
    "pm_application_received": set(),
    "pm_application_rejected": {"team_name"},
    "pm_trial_started_player": {"team_name"},
    "pm_trial_started_team": {"player"},
    "pm_trial_invite": {"team_name"},
    "pm_trial_accepted_team": {"player"},
    "partner_apply_received": {"reference", "organisation"},
    "partner_apply_changes": {"reference", "organisation"},
    "partner_apply_approved": {"reference", "organisation"},
    "partner_apply_rejected": {"reference", "organisation"},
    "event_team_invitation": {"event"},
}


class PlaceholderParityTests(SimpleTestCase):
    """Failure mode 1 and 3: a translated sentence that dropped, renamed, or lost a {placeholder}.

    This is the one most likely to reach a real inbox, because English is the language everyone
    reads back and the other two are the ones nobody re-reads.
    """

    def test_every_template_has_all_three_languages(self):
        for template, langs in COPY.items():
            # Assert: en/fr/pt all present, or copy_for() silently falls back for that locale.
            self.assertEqual(set(langs), set(LANGS), f"COPY[{template}] languages")

    def test_every_subject_has_all_three_languages(self):
        for key, langs in SUBJECTS.items():
            self.assertEqual(set(langs), set(LANGS), f"SUBJECTS[{key}] languages")

    def test_every_template_has_the_same_keys_in_every_language(self):
        for template, langs in COPY.items():
            english = set(langs["en"])
            for lang in ("fr", "pt"):
                # Assert: no key exists in one language only; copy_for()[key] would KeyError.
                self.assertEqual(
                    set(langs[lang]), english,
                    f"COPY[{template}][{lang}] keys differ from English",
                )

    def test_copy_placeholders_match_across_languages(self):
        """THE test the owner asked for: fr/pt must carry exactly the English placeholders."""
        for template, langs in COPY.items():
            for key, english_text in langs["en"].items():
                expected = placeholders(english_text)
                for lang in ("fr", "pt"):
                    self.assertEqual(
                        placeholders(langs[lang][key]), expected,
                        f"COPY[{template}][{lang}][{key}] placeholders differ from English: "
                        f"{langs[lang][key]!r}",
                    )

    def test_subject_placeholders_match_across_languages(self):
        for key, langs in SUBJECTS.items():
            expected = placeholders(langs["en"])
            for lang in ("fr", "pt"):
                self.assertEqual(
                    placeholders(langs[lang]), expected,
                    f"SUBJECTS[{key}][{lang}] placeholders differ from English: {langs[lang]!r}",
                )


class PlaceholderContractTests(SimpleTestCase):
    """Failure mode 2: a sentence uses a {placeholder} its builder does not pass, so .format()
    raises KeyError inside an f-string and the email is never sent."""

    def test_every_copy_template_is_declared(self):
        # Assert: a new template cannot be added without recording what its builder passes.
        self.assertEqual(
            set(COPY), set(CALLER_PLACEHOLDERS),
            "CALLER_PLACEHOLDERS is out of sync with COPY; add the new template's row",
        )

    def test_every_copy_key_is_declared(self):
        for template, langs in COPY.items():
            declared = CALLER_PLACEHOLDERS[template]
            self.assertEqual(
                set(langs["en"]), set(declared),
                f"CALLER_PLACEHOLDERS[{template}] keys are out of sync with COPY[{template}][en]",
            )

    def test_no_sentence_uses_a_placeholder_its_caller_never_passes(self):
        for template, langs in COPY.items():
            for lang in LANGS:
                for key, text in langs[lang].items():
                    allowed = CALLER_PLACEHOLDERS[template][key]
                    unknown = placeholders(text) - allowed
                    self.assertFalse(
                        unknown,
                        f"COPY[{template}][{lang}][{key}] uses {sorted(unknown)}, which the "
                        f"builder does not pass (allowed: {sorted(allowed) or 'none'})",
                    )

    def test_every_subject_is_declared(self):
        self.assertEqual(
            set(SUBJECTS), set(CALLER_SUBJECT_PLACEHOLDERS),
            "CALLER_SUBJECT_PLACEHOLDERS is out of sync with SUBJECTS",
        )

    def test_no_subject_uses_a_placeholder_its_caller_never_passes(self):
        """A subject failure is quieter than a body failure: subject_for() swallows the error and
        ships the literal "{team_name}" in the subject line, which a real person then reads."""
        for key, langs in SUBJECTS.items():
            allowed = CALLER_SUBJECT_PLACEHOLDERS[key]
            for lang in LANGS:
                unknown = placeholders(langs[lang]) - allowed
                self.assertFalse(
                    unknown,
                    f"SUBJECTS[{key}][{lang}] uses {sorted(unknown)}, which the call site does "
                    f"not pass (allowed: {sorted(allowed) or 'none'})",
                )


class RenderSmokeTests(SimpleTestCase):
    """Every sentence must survive .format() with its own placeholders filled, and come out with
    no leftover braces. Catches an unescaped literal brace, a typo'd field name, and a "{}"
    positional field that the catalog is not allowed to use."""

    def test_every_copy_sentence_formats_cleanly(self):
        for template, langs in COPY.items():
            for lang in LANGS:
                for key, text in langs[lang].items():
                    values = {name: f"<{name}>" for name in placeholders(text)}
                    rendered = text.format(**values)
                    self.assertNotIn("{", rendered, f"COPY[{template}][{lang}][{key}]")
                    self.assertNotIn("}", rendered, f"COPY[{template}][{lang}][{key}]")

    def test_every_subject_formats_cleanly(self):
        for key, langs in SUBJECTS.items():
            for lang in LANGS:
                values = {name: f"<{name}>" for name in placeholders(langs[lang])}
                rendered = langs[lang].format(**values)
                self.assertNotIn("{", rendered, f"SUBJECTS[{key}][{lang}]")

    def test_helpers_fall_back_to_english_and_never_raise(self):
        # Arrange: the values user.language really holds in production, including junk.
        for lang in ("", None, "en-GB", "xx", "FR", "pt-BR"):
            # Act + Assert: copy_for always returns a usable dict, subject_for always a string.
            self.assertIn("heading", copy_for("welcome", lang))
            self.assertTrue(subject_for("welcome", lang))
        # Assert: an unknown template/key is empty rather than an exception.
        self.assertEqual(copy_for("no_such_template", "en"), {})
        self.assertEqual(subject_for("no_such_subject", "en"), "")


class BuilderRenderTests(SimpleTestCase):
    """The catalog tests above are static. This one runs the REAL afc_auth builders over the real
    copy, in all three languages, which is the path an actual send takes.

    Every builder here calls c["key"].format(...) inside an f-string with no try/except, so a
    sentence that lost a key or gained a placeholder raises here exactly as it would in production.
    The leftover-token assertion catches the quieter half: a sentence that kept a {placeholder} the
    builder stopped passing would render the literal braces straight into the email.

    The shop / tournament / player-market builders need real model instances and are covered by the
    contract tests above instead.
    """

    LEFTOVER = re.compile(r"\{[a-z_]+\}")

    def _check(self, html, label):
        self.assertNotRegex(html, self.LEFTOVER, f"{label} rendered an unfilled placeholder")
        return html

    def test_account_email_builders_render_in_every_language(self):
        from afc_auth.views import (
            email_change_code, email_email_changed, email_password_changed,
            email_reset_token, email_verification_code, email_welcome,
        )
        for lang in LANGS:
            # Arrange + Act: one call per builder, with the values their call sites really pass.
            html = self._check(email_verification_code("ZeusFF", "483920", lang),
                               f"verification_code/{lang}")
            self.assertIn("483920", html)
            self.assertIn("ZeusFF", html)

            html = self._check(email_welcome("ZeusFF", lang), f"welcome/{lang}")
            self.assertIn("ZeusFF", html)

            self._check(email_reset_token("A1B2C3", lang), f"reset_token/{lang}")

            html = self._check(email_password_changed("ZeusFF", "4 August 2026, 19:12", lang),
                               f"password_changed/{lang}")
            self.assertIn("4 August 2026, 19:12", html)

            self._check(email_change_code("998877", lang), f"change_code/{lang}")

            html = self._check(
                email_email_changed("ZeusFF", "new@example.com", "4 August 2026, 19:12", lang),
                f"email_changed/{lang}")
            self.assertIn("new@example.com", html)

    def test_unknown_language_still_renders_english(self):
        # Assert: a junk User.language (seen in production) degrades to English, never to a crash.
        from afc_auth.views import email_welcome
        self.assertIn("You're in", email_welcome("ZeusFF", "xx"))


class NoDashTests(SimpleTestCase):
    """The owner's loudest standing rule, and these are the strings a stranger reads."""

    # Written as escapes, not as the characters themselves, so this guard does not become the one
    # place in the repo where a grep for a banned dash finds a hit.
    BANNED = ("\u2014", "\u2013")  # em dash, en dash

    def test_no_em_or_en_dashes_in_any_copy(self):
        for template, langs in COPY.items():
            for lang in LANGS:
                for key, text in langs[lang].items():
                    for dash in self.BANNED:
                        self.assertNotIn(
                            dash, text,
                            f"COPY[{template}][{lang}][{key}] contains a banned dash: {text!r}",
                        )

    def test_no_em_or_en_dashes_in_any_subject(self):
        for key, langs in SUBJECTS.items():
            for lang in LANGS:
                for dash in self.BANNED:
                    self.assertNotIn(
                        dash, langs[lang],
                        f"SUBJECTS[{key}][{lang}] contains a banned dash: {langs[lang]!r}",
                    )
