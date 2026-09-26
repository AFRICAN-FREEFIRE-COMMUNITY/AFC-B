"""afc_qr.models - one short, counted link per public page (inbox #46, owner 26 Sep 2026).

A QR code on AFC encodes /q/<token>, never the page address itself, so a scan can be counted before the
visitor is forwarded. There is ONE QrLink per page: everyone who makes a QR for the same team gets the same
token, so every printed poster and shared card adds to the same count.

The target is stored by (target_type, target_id), never by slug or name: a team renamed after its posters
went up still resolves, because the redirect asks the target for its CURRENT address (afc_qr/targets.py).

Connects to: afc_qr/views.py (the four endpoints), afc_qr/targets.py (what each target type means),
frontend components/qr/QrShareButton.tsx (makes the link) and app/q/[token]/route.ts (counts the scan).
"""
from django.conf import settings
from django.db import models


class QrLink(models.Model):
    EVENT = "event"
    TEAM = "team"
    PLAYER = "player"
    NEWS = "news"
    TARGET_TYPES = (EVENT, TEAM, PLAYER, NEWS)

    # q_ + 10 hex (afc_auth.slugs.new_public_token): opaque, says what it is, 40 bits (R22)
    token = models.CharField(max_length=16, unique=True)
    target_type = models.CharField(max_length=10, choices=[(t, t) for t in TARGET_TYPES])
    target_id = models.PositiveIntegerField()
    scan_count = models.PositiveIntegerField(default=0)
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    # Who first asked for it (None when a signed-out visitor did). Informational only: who may SEE the
    # count comes from the target (targets.can_see_stats), never from this field.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="qr_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target_type", "target_id"], name="qr_one_link_per_page"),
        ]

    def __str__(self):
        return f"{self.token} -> {self.target_type} {self.target_id}"
