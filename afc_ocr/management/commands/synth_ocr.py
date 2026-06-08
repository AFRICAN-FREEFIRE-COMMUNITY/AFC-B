"""
afc_ocr/management/commands/synth_ocr.py
================================================================================
manage.py command that builds the OCR character dictionary and generates the
synthetic training corpus (P0: synthetic data + char dictionary).

WHAT IT DOES (delegates to afc_ocr.services.synth)
    1. build_char_dictionary()  -> writes media/models/rec_keys.txt, the PaddleOCR
       recognition character dictionary covering ASCII + every distinct glyph in
       real AFC names (User.username + OCRNameAlias.raw_name).
    2. generate_dataset(count)  -> renders `count` corrupted name crops to
       media/ocr_training/synth/<sha>.png, writes a rec_gt.txt manifest, and
       catalogs each as an OCRTrainingPair(source='synthetic') + OCRCropLabel in
       MySQL, exactly like real admin captures.

USAGE
    python manage.py synth_ocr --count 2000
    python manage.py synth_ocr                 # defaults to 3000
    python manage.py synth_ocr --count 30 --no-dict   # crops only, skip the dict

HOW IT CONNECTS
    This is the entry point a developer (or a future scheduled job) runs to seed
    the self-hosted recognizer's training set before any real labels exist. The
    crops + manifest it writes are consumed by the P2 off-box trainer; the
    rec_keys.txt it writes is consumed by services/local_ocr.LocalOCREngine when a
    fine-tuned bundle is dropped in. All outputs live under media/ (gitignored OCR
    paths) and must never be pushed.

    Mirrors the existing management-command style in the repo
    (afc_player_market/management/commands/seed_countries.py): a BaseCommand with a
    `help` string and SUCCESS-styled stdout reporting.
"""

from django.core.management.base import BaseCommand

from afc_ocr.services.synth import build_char_dictionary, generate_dataset


class Command(BaseCommand):
    help = (
        "Build the OCR character dictionary (media/models/rec_keys.txt) and "
        "generate synthetic name-crop training data "
        "(media/ocr_training/synth/ + OCRTrainingPair/OCRCropLabel rows)."
    )

    def add_arguments(self, parser):
        # --count: how many synthetic crops to render. Default 3000 matches the
        # service default; a small value (e.g. 30) is handy for a quick smoke test.
        parser.add_argument(
            "--count",
            type=int,
            default=3000,
            help="Number of synthetic name crops to generate (default: 3000).",
        )
        # --no-dict: skip rebuilding the char dictionary (e.g. when only topping up
        # crops). The dict is cheap, so it is rebuilt by default.
        parser.add_argument(
            "--no-dict",
            action="store_true",
            help="Skip rebuilding the character dictionary; only generate crops.",
        )
        # --seed: RNG seed for reproducible corruption choices. Default 42 mirrors
        # the service default.
        parser.add_argument(
            "--seed",
            type=int,
            default=42,
            help="Random seed for reproducible corruptions (default: 42).",
        )

    def handle(self, *args, **options):
        count = options["count"]
        seed = options["seed"]

        # ── 1. Character dictionary ──────────────────────────────────────────────
        if not options["no_dict"]:
            self.stdout.write("Building character dictionary from real AFC names...")
            chars = build_char_dictionary(write=True)
            # Report a few stylized (non-ASCII) glyphs we captured so the operator
            # can see the dictionary really covers the community's fancy unicode.
            stylized = [c for c in chars if ord(c) > 127][:20]
            self.stdout.write(
                self.style.SUCCESS(
                    f"Char dictionary: {len(chars)} chars "
                    f"(sample stylized: {' '.join(stylized)})"
                )
            )

        # ── 2. Synthetic crops + catalog rows ────────────────────────────────────
        self.stdout.write(f"Generating {count} synthetic name crops...")
        summary = generate_dataset(n=count, seed=seed)

        self.stdout.write(
            self.style.SUCCESS(
                "Synthetic dataset generated:\n"
                f"  crops written      : {summary['crops_written']}\n"
                f"  skipped (existing) : {summary['skipped_existing']}\n"
                f"  failed             : {summary['failed']}\n"
                f"  OCRTrainingPair    : {summary['pairs_created']}\n"
                f"  OCRCropLabel       : {summary['crop_labels_created']}\n"
                f"  name pool size     : {summary['name_pool_size']}\n"
                f"  manifest           : {summary['manifest_path']}\n"
                f"  out dir            : {summary['out_dir']}\n"
                f"  char dict          : {summary['char_dict_path']}"
            )
        )
