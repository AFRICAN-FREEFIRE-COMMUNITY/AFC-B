# afc_player_market/signals.py
#
# Clean up the on-disk file behind every RecruitmentPostImage when its row goes away (owner 2026-06-29).
# A bare RecruitmentPostImage.delete() removes only the DB row; the underlying MEDIA_ROOT file (under
# recruitment_post_images/) is left orphaned. The edit flow deletes/replaces these rows in several places
# (edit_recruitment_post: remove_image_ids, clear_images, and the implicit replace), remove_post_image
# deletes one, and deleting a RecruitmentPost CASCADEs to its images -- so without this hook every one of
# those paths leaks a file. Hooking post_delete centralises the file removal in ONE place so EVERY current
# and future deletion path (including the cascade, which never calls the view) frees the file too.
#
# The actual file removal mirrors afc_tournament_and_scrims.views (MatchResultImage): img.image.delete(
# save=False) -- delete the storage file without re-saving the (already-deleted) model row.
#
# Wired in afc_player_market/apps.py -> AfcPlayerMarketConfig.ready(). Follows the existing repo signal
# pattern (see afc_team/signals.py, afc_rankings/signals.py).
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import RecruitmentPostImage


@receiver(post_delete, sender=RecruitmentPostImage)
def delete_recruitment_post_image_file(sender, instance, **kwargs):
    """Remove the screenshot's storage file after its row is deleted (any path: edit-remove,
    clear, replace, remove_post_image, or a RecruitmentPost cascade delete)."""
    # Best-effort: a storage hiccup must never break the delete/cascade that already committed.
    # delete(save=False) drops the file without touching the (gone) DB row; guard the empty-field case.
    try:
        if instance.image:
            instance.image.delete(save=False)
    except Exception:
        pass
