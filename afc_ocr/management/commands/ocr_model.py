"""
afc_ocr/management/commands/ocr_model.py
================================================================================
manage.py command to drive the OCR blue-green MODEL SWAP and inspect its state
(OCR learning loop, Phase 4). Thin CLI over afc_ocr.services.model_registry.

WHAT IT DOES (delegates to afc_ocr.services.model_registry)
    --promote <version>  -> promote(version): flip the live student model to
                            media/models/student_v<version>/ atomically, then reset the
                            in-process engine so it reloads WITHOUT a Django restart.
    --rollback           -> rollback(): instantly revert to the previously live model
                            (or cold-start / Gemini-only if there is nothing to revert to).
    --status             -> print the active model dir + version and recent shadow stats
                            (how the student has been comparing to admin-committed truth).

USAGE
    python manage.py ocr_model --status
    python manage.py ocr_model --promote 9
    python manage.py ocr_model --rollback

HOW IT CONNECTS
    The admin (or the validated retrain pipeline) runs this to put a freshly trained,
    off-box student bundle into service, or to pull a bad one out. promote/rollback flip
    media/models/current (the pointer services/local_ocr should resolve via
    model_registry.active_model_dir()) and reset local_ocr._ENGINE. --status reads the
    shadow log model_registry.record_shadow writes from the OCR commit path. All artifacts
    live under media/ (gitignored OCR paths) and must never be pushed.

    Mirrors the existing management-command style in this app
    (afc_ocr/management/commands/synth_ocr.py): a BaseCommand with a `help` string,
    argparse flags, and SUCCESS/WARNING/ERROR-styled stdout reporting.
"""

from django.core.management.base import BaseCommand, CommandError

from afc_ocr.services import model_registry


class Command(BaseCommand):
    help = (
        "Manage the self-hosted OCR student model (blue-green swap): "
        "--promote <version>, --rollback, or --status."
    )

    def add_arguments(self, parser):
        # The three actions are mutually exclusive: exactly one of promote/rollback/status.
        # A mutually-exclusive, required group makes argparse enforce that for us and
        # print a clean usage error otherwise.
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--promote",
            metavar="VERSION",
            help="Promote media/models/student_v<VERSION>/ to be the live model.",
        )
        group.add_argument(
            "--rollback",
            action="store_true",
            help="Revert to the previously live model (or cold-start if none).",
        )
        group.add_argument(
            "--status",
            action="store_true",
            help="Print the active model and recent shadow-comparison stats.",
        )

    def handle(self, *args, **options):
        if options.get("promote") is not None:
            self._promote(options["promote"])
        elif options.get("rollback"):
            self._rollback()
        else:
            self._status()

    # ── --promote ───────────────────────────────────────────────────────────
    def _promote(self, version):
        """Flip the live model to student_v<version>. Surfaces a clean CommandError if the
        bundle is not on disk (model_registry.promote raises FileNotFoundError)."""
        try:
            target = model_registry.promote(version)
        except FileNotFoundError as exc:
            # Re-raise as a CommandError so manage.py prints a tidy message + non-zero exit,
            # rather than a traceback. The admin then knows to deploy the bundle first.
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Promoted. Active model is now: {target}"))
        self.stdout.write(
            "In-process engine reset; the next OCR request rebuilds against the new model "
            "(no restart needed)."
        )

    # ── --rollback ────────────────────────────────────────────────────────────
    def _rollback(self):
        """Revert to the previous model, or clear to cold-start when there is nothing safe
        to fall back to (model_registry.rollback returns None in that case)."""
        target = model_registry.rollback()
        if target:
            self.stdout.write(self.style.SUCCESS(f"Rolled back. Active model is now: {target}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No valid previous model; pointer cleared. OCR is now cold-start "
                    "(Gemini-only) until a model is promoted."
                )
            )

    # ── --status ──────────────────────────────────────────────────────────────
    def _status(self):
        """Print the active model + recent shadow stats. Always prints something sensible,
        including the cold-start case (no model deployed yet)."""
        active_dir = model_registry.active_model_dir()
        active_ver = model_registry.active_version()
        stats = model_registry.recent_shadow_stats()

        self.stdout.write(self.style.SUCCESS("OCR student model status"))
        if active_dir:
            self.stdout.write(f"  active model dir : {active_dir}")
            self.stdout.write(f"  active version   : {active_ver}")
        else:
            # Cold start is a normal state, not an error: the router runs Gemini-only.
            self.stdout.write(
                self.style.WARNING("  active model     : NONE (cold start; router is Gemini-only)")
            )

        # Shadow stats: how the (shadow) student has compared to admin-committed truth.
        # None / 0 when no shadow comparisons have been logged yet.
        self.stdout.write("  recent shadow comparisons:")
        self.stdout.write(f"    count          : {stats['count']}")
        self.stdout.write(f"    mean exact-match: {stats['mean_exact_match']}")
        self.stdout.write(f"    perfect reads  : {stats['perfect_count']}")
        self.stdout.write(f"    perfect rate   : {stats['perfect_rate']}")
