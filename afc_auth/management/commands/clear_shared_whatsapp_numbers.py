"""Clear the WhatsApp number from every account that shares it with another account.

Owner, 2026-09-22, on the 52 shared numbers found across 110 accounts: "remove all the numbers from
those accounts so they have to reenter them."

WHY CLEAR RATHER THAN PICK A WINNER
-----------------------------------
The number is how the site sends recovery codes and WhatsApp sign-in codes. Two accounts on one line
means whoever holds that phone can receive a code for an account that is not theirs, and the
recovery flow has to guess which account a number belongs to. From the outside we cannot tell a
family sharing one handset from a duplicate account, so keeping "the oldest" would be a guess with
somebody's account security as the stake. Clearing costs the real owner ten seconds of retyping.

WHAT IT DOES
------------
For every number held by more than one CANONICAL profile (afc_auth.models.canonical_profile, the row
every reader and writer agrees on):
  * blanks whatsapp_number, whatsapp_number_updated_at and whatsapp_opt_in on each of them,
  * writes one notification per affected user saying what happened and where to fix it, with a deep
    link to the profile settings page (target_type/target_id, so the bell shows "Take me there"),
  * prints the before and after counts.

Nobody is emailed: the number they would be reached on is the field in question, and an email about
a phone number people share on purpose reads like a security alarm. The bell is the honest channel.

Run:
    python manage.py clear_shared_whatsapp_numbers --dry-run     # count only, changes nothing
    python manage.py clear_shared_whatsapp_numbers               # do it

Idempotent: a second run finds nothing shared and clears nothing. Pairs with
identifiers.py section 4 (inbox #21), which stops new duplicates at every write path.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from afc_auth.models import Notifications
from afc_auth.identifiers import shared_whatsapp_numbers

NOTICE_TITLE = "Your WhatsApp number was removed"
NOTICE_MESSAGE = (
    "Another AFC account had the same WhatsApp number as yours. A number can only be on one "
    "account, because it is how we send sign-in and recovery codes, so we removed it from both. "
    "Add your own number again in your profile settings."
)


class Command(BaseCommand):
    help = "Clear WhatsApp numbers that are shared between accounts (owner 2026-09-22)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="count what would change and write nothing")
        parser.add_argument("--quiet-notify", action="store_true",
                            help="clear the numbers but do not create the notifications")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        shared = shared_whatsapp_numbers()
        accounts = sum(len(profiles) for profiles in shared.values())

        self.stdout.write("shared numbers: %d, accounts on them: %d" % (len(shared), accounts))
        if not shared:
            self.stdout.write(self.style.SUCCESS("nothing to clear"))
            return
        if dry:
            for number, profiles in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:10]:
                masked = number[:4] + "*" * max(0, len(number) - 8) + number[-4:]
                self.stdout.write("  %s held by %d accounts" % (masked, len(profiles)))
            self.stdout.write(self.style.WARNING("dry run: nothing written"))
            return

        cleared = 0
        notified = 0
        with transaction.atomic():
            for _number, profiles in shared.items():
                for profile in profiles:
                    profile.whatsapp_number = ""
                    profile.whatsapp_number_updated_at = None
                    profile.whatsapp_opt_in = False
                    profile.save(update_fields=["whatsapp_number", "whatsapp_number_updated_at",
                                                "whatsapp_opt_in"])
                    cleared += 1
                    if options["quiet_notify"] or profile.user is None:
                        continue
                    Notifications.objects.create(
                        user=profile.user,
                        title=NOTICE_TITLE,
                        message=NOTICE_MESSAGE,
                        notification_type="whatsapp_number_cleared",
                        # The bell's "Take me there" opens the profile settings, which is the one
                        # place they can put the number back.
                        target_type="profile_settings",
                        target_id="whatsapp",
                    )
                    notified += 1

        self.stdout.write(self.style.SUCCESS(
            "cleared %d accounts, notified %d" % (cleared, notified)))
        left = shared_whatsapp_numbers()
        self.stdout.write("shared numbers left: %d" % len(left))
