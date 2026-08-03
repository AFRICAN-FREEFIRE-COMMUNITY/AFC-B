# ──────────────────────────────────────────────────────────────────────────────
# "This AFC account is gone, delete your copy."
#
# The other half of the deletion signal. afc_sso/api.py handles the player pressing
# Remove for ONE partner; this handles the whole account going away, which means telling
# EVERY partner the player was connected to.
#
# WHY A pre_delete RECEIVER RATHER THAN AN ENDPOINT: AFC has no delete-my-account endpoint
# today. A receiver covers every route the row can actually disappear by - the Django
# admin, a shell, a management command, and any endpoint added later - without that future
# endpoint having to remember to call anything. It also means the signal cannot be
# forgotten in a code path nobody thought about.
#
# WHY pre_delete AND NOT post_delete: the pairwise sub is derived from the user's primary
# key (afc_sso/validators.py pairwise_sub), so the payload can only be built while the row
# still exists. By post_delete there is nothing left to derive it from.
#
# COST WHEN NOTHING IS CONNECTED: two indexed queries by user id, then nothing. A partner
# with no deletion_webhook_url is skipped without work, so the ordinary case of deleting a
# test account costs a pair of empty lookups.
#
# Registered in afc_sso/apps.py ready().
# ──────────────────────────────────────────────────────────────────────────────
import logging

from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from oauth2_provider.models import get_application_model

from .webhooks import REASON_ACCOUNT_DELETED, connected_application_ids, notify_disconnected

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL, dispatch_uid="afc_sso_account_deleted")
def notify_partners_of_account_deletion(sender, instance, **kwargs):
    """Signal every partner holding this player's data that the account is being deleted.

    Best effort by design, exactly like the revoke path: deleting an AFC account must
    succeed whether or not a partner's server is reachable, so notify_disconnected
    swallows its own failures and the loop below cannot raise. A partner that misses the
    signal keeps a stale record, which is a problem worth retrying (afc_sso/tasks.py) but
    not one worth blocking an account deletion over.
    """
    try:
        application_ids = connected_application_ids(instance)
        if not application_ids:
            return

        # Only partners that asked for a signal. Everyone else has nothing to receive it.
        applications = (
            get_application_model().objects
            .filter(pk__in=application_ids)
            .exclude(deletion_webhook_url="")
        )
        for application in applications:
            notify_disconnected(application, instance, REASON_ACCOUNT_DELETED)
    except Exception as exc:  # noqa: BLE001 - an account deletion must never fail here
        logger.warning(
            "sso webhook: could not signal account deletion for user #%s: %s",
            getattr(instance, "pk", None), exc,
        )
