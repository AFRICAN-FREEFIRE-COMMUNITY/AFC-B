"""
afc_partner_apply.models - organisations apply to become AFC partners, in public.

WHY THIS APP EXISTS
    The owner, 2026-08-04: "the partner sends you their details and I have to input them on my
    end? HOW CAN WE AUTOMATE THIS?" Until now an organisation that wanted "Sign in with AFC" or a
    Data API key emailed their redirect URIs and their logo, and the owner retyped all of it at
    /a/partners. Retyping is where the mistakes were: a mistyped redirect URI is a sign-in that
    fails at the worst moment, and a mistyped contact email is a partner who never hears back.

    So the organisation now fills the form themselves, their own values are validated AGAINST THE
    REAL RULES while they are still sitting there to fix them, and the only thing left for a human
    is the decision that actually needs one: do I trust this organisation, and with what.

WHAT IS DELIBERATELY *NOT* AUTOMATED
    The grant. An applicant never asks for a scope. See `use_case` and `data_needed` below, and
    the long comment on why, which is the single most important design decision in this app.

THE SHAPE
    PartnerApplication ──approved──▶ afc_sso.AFCSSOApplication   (the "Sign in with AFC" product)
                       ──approved──▶ afc_partner_api.Partner      (the Data API product)
                       ──approved──▶ a one-time credential claim link, never a secret in an email

HOW IT CONNECTS
    - Written by afc_partner_apply/views_public.py (an ANONYMOUS write, hence the rate limiting and
      the hashed IP, both modelled on afc_feedback which does the same thing for site feedback).
    - Read and decided by afc_partner_apply/views_admin.py, behind head_admin / partner_admin.
    - Approval provisions through afc_sso/provisioning.py provision_sso_application and
      afc_partner_api's Partner + PartnerApiKey, never through a private copy of either.
    - Frontend: app/(root)/partners/apply/page.tsx (the form), .../apply/status/page.tsx (the
      applicant's own view), and the "Applications" tab of app/(a)/a/partners/page.tsx.
    - Emails go out through afc_auth.views.send_email with hand-authored en/fr/pt copy from
      afc_auth.email_i18n, in the language the applicant filled the form in.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

# Reference codes an applicant reads out over the phone or pastes into an email. No I, O, 0 or 1:
# every character survives being spoken aloud or copied from a screenshot.
_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REFERENCE_LENGTH = 6
REFERENCE_PREFIX = "AFC-P-"

# How long an approved applicant has to collect their credentials before the link goes stale.
# Long enough to survive a weekend and a timezone, short enough that a forwarded email is not a
# standing invitation. The owner can always mint a fresh one from the admin page.
CLAIM_WINDOW_HOURS = 72


def generate_reference():
    """A short, unique, human-speakable handle for one application, e.g. "AFC-P-7K3MQX".

    Collision handling is a retry loop rather than a uniqueness suffix: at 32^6 (about a billion)
    combinations a clash is vanishingly rare, and a retry keeps every reference the same shape,
    which a "-2" suffix would not.
    """
    while True:
        code = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(_REFERENCE_LENGTH))
        reference = f"{REFERENCE_PREFIX}{code}"
        if not PartnerApplication.objects.filter(reference=reference).exists():
            return reference


def hash_token(token):
    """Salted one-way hash of an access or claim token.

    The SAME reasoning as PartnerApiKey.key_hash and afc_feedback's ip_hash: AFC needs to
    RECOGNISE the token when it comes back, never to reproduce it. A database leak therefore
    hands nobody a working link. SECRET_KEY is the salt, so a hash is meaningless outside this
    deployment.
    """
    return hashlib.sha256(f"{settings.SECRET_KEY}:{token}".encode("utf-8")).hexdigest()


def generate_token():
    """A URL-safe bearer token for an application link. 32 bytes of entropy, so guessing one is
    not a strategy."""
    return secrets.token_urlsafe(32)


class PartnerApplication(models.Model):
    """One organisation asking to become an AFC partner.

    STATUSES, and why there are four rather than three:
        pending           submitted, waiting on the owner.
        changes_requested the owner wants something fixed (a redirect URI, a vague answer) WITHOUT
                          rejecting. The applicant edits in place and it returns to pending. This
                          is the state that stops "your URI has a typo" costing an organisation a
                          rejection and a fresh application.
        approved          provisioned. `sso_application` and/or `data_partner` now point at what
                          was created, and a claim link has been issued.
        rejected          declined, with a reason. TERMINAL: a rejected application is never
                          reopened, because "we said no and then changed the row" is not an audit
                          trail. The organisation may apply again from scratch, which is a new row
                          with its own reference, and the review screen shows the owner how many
                          earlier applications that contact email has (see views_admin).
    """

    PENDING = "pending"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (CHANGES_REQUESTED, "Changes requested"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    ]

    id = models.AutoField(primary_key=True)

    # The applicant's public handle for their own application. In the status URL and in every
    # email. Unique, and never reused.
    reference = models.CharField(max_length=20, unique=True, db_index=True)

    # ── Who is applying ──
    organisation_name = models.CharField(max_length=160)
    # What a PLAYER would see on the consent screen. Optional: most organisations want their own
    # name, and the owner can override it at review time anyway.
    display_name = models.CharField(max_length=120, blank=True, default="")
    homepage_url = models.URLField()
    # Free text, not a choice: AFC's country list is a frontend concern and an applicant outside it
    # should not be blocked from applying.
    country = models.CharField(max_length=80, blank=True, default="")

    contact_name = models.CharField(max_length=120)
    # Where every decision email goes, and the key the review screen counts earlier applications
    # by. Indexed for exactly that lookup.
    contact_email = models.EmailField(db_index=True)
    contact_role = models.CharField(max_length=120, blank=True, default="")
    # A WhatsApp number AFC can actually message (owner 2026-08-04: "someone could get messaged on
    # it"), so it is stored in E.164 and nothing else. That is a DELIBERATE break from the two
    # phone fields already in this codebase: UserProfile.whatsapp_number (afc_auth/models.py) and
    # Vendor.whatsapp_number (afc_shop/models.py) both store whatever was typed and normalise at
    # send time, and the result is that 34 of 133 stored player numbers are in a local form that
    # cannot be resolved without a country. Copying that pattern here would reproduce the same
    # unreachable rows. The applicant's own browser already hands us E.164 (the form uses the
    # existing PhoneNumberInput, which emits it), and views_public runs afc_whatsapp.phone.to_e164
    # on the way in and refuses what it cannot resolve, so a number stored here is one that can be
    # dialled. Optional: an applicant who would rather only be emailed leaves it blank.
    # 32 rather than E.164's real 16-character ceiling, matching Vendor's headroom.
    contact_whatsapp = models.CharField(max_length=32, blank=True, default="")

    # ── Which product, or both ──
    # NOT a trust decision, just routing: it decides which fields the form asks for next and which
    # thing approval provisions. At least one is enforced in the view.
    wants_sso = models.BooleanField(default=False)
    wants_data_api = models.BooleanField(default=False)

    # ── The technical values that used to be retyped by hand ──
    # Whitespace-separated, exactly as django-oauth-toolkit stores them, because these are handed
    # to provision_sso_application unchanged. They are validated at SUBMIT time against
    # afc_sso/redirect_policy.py, so an applicant who typed a wildcard or a query string finds out
    # while they are still looking at the form.
    redirect_uris = models.TextField(blank=True, default="")
    post_logout_redirect_uris = models.TextField(blank=True, default="")
    deletion_webhook_url = models.URLField(blank=True, default="")

    # AFC's own copy of the applicant's mark, validated by afc_sso/provisioning.py
    # _clean_logo_upload (Pillow-verified, renamed, 2 MB) because it may end up rendered on the
    # consent screen. Optional: an organisation without a logo file is not blocked from applying,
    # and the owner can upload one later from the SSO edit form.
    logo = models.ImageField(upload_to="partner_application_logos/", null=True, blank=True)

    # ── The two questions the decision actually turns on ──
    #
    # THE APPLICANT DOES NOT REQUEST SCOPES, AND THIS IS DELIBERATE.
    # It would have been easy to put the eight share_* toggles on the public form as checkboxes.
    # It would also have been wrong, for three reasons:
    #   1. A checkbox list trains an applicant to tick everything. Nobody reads "Free Fire UID" and
    #      thinks "we do not need that"; they think "we might". The owner would then be reviewing a
    #      maximal request every time, which is the same as reviewing nothing.
    #   2. It reframes the grant as an entitlement. An application that SAYS "we want email, team,
    #      ranking" invites the answer "approved as requested". An application that says "we show a
    #      player's rank on their profile page" invites the answer "then you need afc.ranking and
    #      nothing else", which is the least-privilege outcome AFC's whole model is built for.
    #   3. AFC's toggles are not the applicant's vocabulary. "afc.standing" means something precise
    #      here and nothing at all to somebody integrating for the first time.
    # So the applicant describes the product and the need in their own words, and the owner turns
    # that into toggles on the review screen, where the toggles live and where every one of them
    # still starts OFF.
    use_case = models.TextField()      # what are you building, and who uses it
    data_needed = models.TextField()   # what do you need from AFC, and why

    # ── Applicant-side plumbing ──
    # The language the form was filled in. Every email to this applicant is sent in it, so a
    # French organisation is not answered in English. Same field, same purpose, as
    # FeedbackSubmission.locale.
    locale = models.CharField(max_length=8, blank=True, default="")
    # Salted hash of the applicant's long-lived access token: their key to their own status page
    # and, while changes are requested, to editing their answers. The reference alone is NOT
    # enough to read an application, because it travels in emails and gets forwarded.
    access_token_hash = models.CharField(max_length=64, db_index=True, blank=True, default="")
    # Anonymous write, so the sender is identified for rate limiting the same way afc_feedback
    # does it: a salted hash, never the address itself.
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # ── Decision ──
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    # The owner's message TO THE APPLICANT. Shown on the status page and sent in the decision
    # email, so it is written to be read by them: a rejection reason, or what to fix.
    decision_note = models.TextField(blank=True, default="")
    # The owner's note TO AFC. Never serialized to the applicant, by any endpoint.
    internal_note = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="partner_applications_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # ── What approval produced ──
    # SET_NULL rather than CASCADE: deleting a provisioned partner must not delete the record that
    # AFC approved them, which is the only history of the decision.
    sso_application = models.ForeignKey(
        "afc_sso.AFCSSOApplication", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_applications",
    )
    data_partner = models.ForeignKey(
        "afc_partner_api.Partner", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_applications",
    )

    # ── The credential claim (see views_public.claim_credentials) ──
    # AFC NEVER EMAILS A CLIENT SECRET. The approval email carries a single-use link instead; the
    # secret is minted when that link is opened and shown exactly once, on one page, to whoever
    # opened it. Only the hash of the claim token is stored, so this row cannot be used to work
    # out the link, and the window closes on its own.
    claim_token_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest first: the queue is read top-down, like the feedback queue.
        ordering = ["-created_at"]
        indexes = [
            # The admin list filters by status and always sorts by recency.
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} {self.organisation_name} ({self.status})"

    # ── Small predicates, so views and templates never re-derive the same condition ──

    def is_open(self):
        """Still in the applicant's hands or the owner's, as opposed to decided."""
        return self.status in (self.PENDING, self.CHANGES_REQUESTED)

    def is_editable_by_applicant(self):
        """Only while the owner has explicitly asked for changes.

        A pending application is NOT editable: the owner may be reading it at that moment, and an
        answer that changes under them is worse than a second application.
        """
        return self.status == self.CHANGES_REQUESTED

    def claim_is_open(self):
        """True when a credential claim link exists, has not been used, and has not expired.

        All three conditions in one place because the claim endpoint, the status serializer and
        the admin serializer each need the same answer and must not disagree about it.
        """
        if not self.claim_token_hash or self.claimed_at is not None:
            return False
        return bool(self.claim_expires_at and self.claim_expires_at > timezone.now())

    def issue_claim_token(self):
        """Mint a fresh single-use credential link. Returns the PLAINTEXT token, once.

        Called on approval and again whenever the owner presses "Send a new credentials link".
        Re-issuing DELIBERATELY resets `claimed_at`: the owner is saying "let them collect
        credentials again", and the claim endpoint rotates the secret rather than revealing the
        old one, so an earlier claim is not undone by a later one.
        """
        token = generate_token()
        self.claim_token_hash = hash_token(token)
        self.claim_expires_at = timezone.now() + timezone.timedelta(hours=CLAIM_WINDOW_HOURS)
        self.claimed_at = None
        self.save(update_fields=["claim_token_hash", "claim_expires_at", "claimed_at", "updated_at"])
        return token

    def issue_access_token(self):
        """Mint the applicant's long-lived key to their own application. Returns the plaintext.

        Issued once, at submission, and carried in the status link in every email. It is not
        rotated on each email because an applicant who bookmarks the link should keep it working;
        it is worth exactly one application's own data and nothing else.
        """
        token = generate_token()
        self.access_token_hash = hash_token(token)
        self.save(update_fields=["access_token_hash", "updated_at"])
        return token
