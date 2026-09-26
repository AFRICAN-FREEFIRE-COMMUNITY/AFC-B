"""afc_referrals.models - referral programs (inbox #47, owner 26 Sep 2026; spec
docs/superpowers/specs/2026-09-26-referral-programs-design.md).

An admin runs a PROGRAM: who may refer (everyone, some countries, some teams, chosen users), when, what
counts as a referral (a verified signup, joining or creating a team, registering for an event, a first
shop purchase) and what it pays (milestones, top referrers at the end, a welcome prize for the new
person). Each eligible user gets a CODE per program; a visit to /r/<code> is a CLICK; a new account that
arrives through it is a REFERRAL (pending, then counted when the rule is met); prizes become REWARDS that
staff deliver.

How it connects:
  - engine.py        the only place that decides eligibility, claims, counting and awards
  - signals.py       calls engine.on_action from the real write paths (verification, team membership,
                     event registration, paid orders), so no view has to remember to
  - views.py         the endpoints (public landing + click, the user's claim and profile card, admin)
  - frontend         app/r/[code], the profile Referrals card, app/(a)/a/referrals
Every row that appears in a URL carries an opaque token or a slug, never its id (R22).
"""
from django.conf import settings
from django.db import models

from afc_auth.slugs import ensure_public_token


class ReferralProgram(models.Model):
    SCOPE_EVERYONE = "everyone"
    SCOPE_COUNTRIES = "countries"
    SCOPE_TEAMS = "teams"
    SCOPE_USERS = "users"
    SCOPES = (SCOPE_EVERYONE, SCOPE_COUNTRIES, SCOPE_TEAMS, SCOPE_USERS)

    RULE_SIGNUP = "signup"      # the new account is verified (email confirmed, or Google / Discord)
    RULE_TEAM = "team"          # the new person joins or creates a team
    RULE_EVENT = "event"        # the new person registers for an event (count_event, or any)
    RULE_PURCHASE = "purchase"  # the new person's first paid shop order
    RULES = (RULE_SIGNUP, RULE_TEAM, RULE_EVENT, RULE_PURCHASE)

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    # A draft is invisible to users and counts nothing, whatever its dates say
    is_published = models.BooleanField(default=False)

    scope = models.CharField(max_length=12, choices=[(s, s) for s in SCOPES], default=SCOPE_EVERYONE)
    # Country names as the rest of the site stores them (User.country / ip_country). A region picked in
    # the admin form is expanded into its countries there, the way event geo-restrictions are.
    countries = models.JSONField(default=list, blank=True)
    teams = models.ManyToManyField("afc_team.Team", blank=True, related_name="referral_programs")
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="referral_programs")

    count_rule = models.CharField(max_length=10, choices=[(r, r) for r in RULES], default=RULE_SIGNUP)
    # For RULE_EVENT: only this event counts. Empty = any event.
    count_event = models.ForeignKey("afc_tournament_and_scrims.Event", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    # Optional shared code (e.g. BOUNTY26) credited to the program itself rather than a person
    program_code = models.CharField(max_length=20, blank=True, default="")

    ranks_awarded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return self.name


class ProgramPrize(models.Model):
    KIND_MILESTONE = "milestone"  # the referrer reaches `threshold` counted referrals
    KIND_RANK = "rank"            # the referrer finishes at `rank` when the program ends
    KIND_WELCOME = "welcome"      # the referred person, once their referral counts
    KINDS = (KIND_MILESTONE, KIND_RANK, KIND_WELCOME)

    TYPE_SHOP_ITEM = "shop_item"
    TYPE_DIAMONDS = "diamonds"
    TYPE_COUPON = "coupon"
    TYPE_CASH = "cash"
    TYPE_CUSTOM = "custom"
    TYPES = (TYPE_SHOP_ITEM, TYPE_DIAMONDS, TYPE_COUPON, TYPE_CASH, TYPE_CUSTOM)

    program = models.ForeignKey(ReferralProgram, on_delete=models.CASCADE, related_name="prizes")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in KINDS])
    threshold = models.PositiveIntegerField(null=True, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    prize_type = models.CharField(max_length=10, choices=[(t, t) for t in TYPES])
    # shop_item / diamonds: the variant staff hand over (price and diamonds come from the shop, R73)
    product_variant = models.ForeignKey("afc_shop.ProductVariant", null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    coupon_discount_type = models.CharField(max_length=10, blank=True, default="")  # percent | fixed
    coupon_discount_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cash_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # USD
    custom_text = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["kind", "threshold", "rank", "id"]


class ReferralCode(models.Model):
    program = models.ForeignKey(ReferralProgram, on_delete=models.CASCADE, related_name="codes")
    # Null for the program's own shared code
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                             related_name="referral_codes")
    code = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["program", "user"], name="ref_one_code_per_user")]


class ReferralClick(models.Model):
    public_token = models.CharField(max_length=16, unique=True, blank=True)
    code = models.ForeignKey(ReferralCode, on_delete=models.CASCADE, related_name="clicks")
    ip_hash = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = ensure_public_token(self, "c", update_fields=kwargs.get("update_fields"))
        if kwargs["update_fields"] is None:
            kwargs.pop("update_fields")
        super().save(*args, **kwargs)


class Referral(models.Model):
    PENDING = "pending"
    COUNTED = "counted"
    REJECTED = "rejected"
    FLAGGED = "flagged"   # held for an admin; counts only if an admin says so
    STATUSES = (PENDING, COUNTED, REJECTED, FLAGGED)

    public_token = models.CharField(max_length=16, unique=True, blank=True)
    program = models.ForeignKey(ReferralProgram, on_delete=models.CASCADE, related_name="referrals")
    code = models.ForeignKey(ReferralCode, on_delete=models.CASCADE, related_name="referrals")
    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="referrals_made")
    # One referral per account, ever: when programs overlap the first claim wins ("one program per signup")
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                    related_name="referral_received")
    click = models.ForeignKey(ReferralClick, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, choices=[(s, s) for s in STATUSES], default=PENDING)
    reason = models.CharField(max_length=40, blank=True, default="")
    ip_hash = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    counted_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = ensure_public_token(self, "r", update_fields=kwargs.get("update_fields"))
        if kwargs["update_fields"] is None:
            kwargs.pop("update_fields")
        super().save(*args, **kwargs)


class Reward(models.Model):
    PENDING = "pending"        # owed; staff deliver it (or it is a coupon, delivered at once)
    DELIVERED = "delivered"    # handed over (cash: paid)
    CANCELLED = "cancelled"
    STATUSES = (PENDING, DELIVERED, CANCELLED)

    public_token = models.CharField(max_length=16, unique=True, blank=True)
    program = models.ForeignKey(ReferralProgram, on_delete=models.CASCADE, related_name="rewards")
    prize = models.ForeignKey(ProgramPrize, on_delete=models.CASCADE, related_name="rewards")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_rewards")
    # Set for a welcome prize (one per referral); milestone and rank prizes are one per user
    referral = models.ForeignKey(Referral, null=True, blank=True, on_delete=models.CASCADE, related_name="rewards")
    status = models.CharField(max_length=10, choices=[(s, s) for s in STATUSES], default=PENDING)
    coupon = models.ForeignKey("afc_shop.Coupon", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")

    # Awarding is idempotent: "<prize>:u<user>" for milestone and rank prizes, "<prize>:r<referral>" for a
    # welcome prize, and the database refuses a second row. A plain unique column rather than a
    # conditional constraint, because MySQL ignores conditional unique constraints.
    award_key = models.CharField(max_length=40, unique=True)

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = ensure_public_token(self, "w", update_fields=kwargs.get("update_fields"))
        if kwargs["update_fields"] is None:
            kwargs.pop("update_fields")
        super().save(*args, **kwargs)
