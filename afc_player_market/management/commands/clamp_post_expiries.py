# afc_player_market/management/commands/clamp_post_expiries.py
# ──────────────────────────────────────────────────────────────────────────────
# One-off backfill for the one-month expiry cap (feature "L-market-expiry-cap").
#
# WHY THIS EXISTS:
#   create_recruitment_post / edit_recruitment_post now cap a post's expiry at one
#   calendar month (see afc_player_market.views.add_one_month). But posts created
#   BEFORE that cap landed can hold a far-future post_expiry_date (2027, December, …),
#   so they would never auto-close (RecruitmentPost.is_active = post_expiry_date >=
#   today). This command pulls every such row back in line: it sets the expiry to
#   add_one_month(created_at.date()), the longest life the cap allows, measured from
#   the post's OWN start, exactly like edit_recruitment_post caps an edit.
#
#   It is the team-consistent way to apply the fix because migrations are gitignored
#   in this repo, so a data migration would not travel to other clones, whereas a
#   management command does.
#
# HOW TO RUN (from the backend dir, with the venv active):
#   python manage.py clamp_post_expiries
#
# SAFE TO RE-RUN: idempotent. It only touches rows whose expiry is STILL beyond the
# one-month cap; a row already at or under the cap is skipped, so a second run reports
# "0 clamped". It mutates only post_expiry_date (no other field, no deletes).
#
# CONNECTS TO: afc_player_market.views.add_one_month (the single source of the cap),
# RecruitmentPost (the rows it backfills), and the FE/BE create+edit guards that keep
# new posts inside the cap going forward.
# ──────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand

from afc_player_market.models import RecruitmentPost
# Reuse the SAME cap helper the views use so the backfill and the live guards can never
# drift apart (one definition of "one calendar month, day-clamped").
from afc_player_market.views import add_one_month


class Command(BaseCommand):
    help = "Clamp any RecruitmentPost expiry that is beyond one month of its start date down to the one-month cap."

    def handle(self, *args, **kwargs):
        clamped = 0

        # iterator() keeps memory flat if the table is large; we only read created_at +
        # post_expiry_date and write post_expiry_date, so no related lookups are needed.
        for post in RecruitmentPost.objects.all().iterator():
            cap = add_one_month(post.created_at.date())  # longest expiry this post may hold
            if post.post_expiry_date > cap:
                post.post_expiry_date = cap
                # update_fields keeps the write surgical (only the one column) and avoids
                # tripping auto_now/auto_now_add columns.
                post.save(update_fields=["post_expiry_date"])
                clamped += 1

        self.stdout.write(self.style.SUCCESS(f"Clamped {clamped} recruitment post(s) to the one-month expiry cap."))
