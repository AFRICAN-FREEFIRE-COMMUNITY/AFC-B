"""
manage.py bootstrap_ocr  --  cold-start the OCR model from a FOLDER of loose screenshots.

Turns a directory of Free Fire result screenshots into GOLD recognition training data,
using Gemini as the verifier. This is the seed dataset (P0 cold start) for the very first
fine-tune, BEFORE any organizer has used the live review flow. No Match row, no registered
players, and no name/team database are needed: we only capture RECOGNITION truth (what the
pixels say), which is all the recognizer learns. Identity (which player a name maps to) is a
separate, later concern handled at leaderboard-commit time.

PER IMAGE:
  1. Recognize: the local PP-OCR detector (services/local_ocr) returns every text box with
     its crop region + a candidate read.
  2. Verify: Gemini reads the whole screenshot and returns the authoritative names + kills.
  3. Align: each detected NAME box is fuzzy-matched to a Gemini name. On a confident match we
     save the cropped box image + the GEMINI name as the gold label (Gemini corrects the
     stylized glyphs the raw detector misreads). Boxes that match nothing, or Gemini names
     with no box, are recorded as DISAGREEMENTS for human review (--report shows them).
  4. Persist: one OCRTrainingPair(source='gemini_autolabel', match=None) per image + one
     OCRCropLabel(field='name', crop_path=<saved crop>, text=<gemini name>) per aligned crop,
     plus the whole screenshot content-addressed under media/ocr_training/. These rows feed
     services/dataset.assemble_rec_dataset exactly like live captures.

USAGE:
  manage.py bootstrap_ocr --folder "C:/path/to/freefire screenshots"
  manage.py bootstrap_ocr --folder "<path>" --event-type team --limit 5 --dry-run

REQUIRES a WORKING settings.GEMINI_API_KEY (the verifier). Without it the command stops with
a clear message (the old key was leaked + revoked; generate a new one).

CONSUMED BY: this is a one-off operator command. Its output (OCRTrainingPair/OCRCropLabel +
crops) is then exported by /events/ocr/dataset-export/ and fine-tuned by the train cycle.
"""

import hashlib
import os
import uuid

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

# Recognition uses the same detector the live engine uses, so the crops we train on match the
# crops we will infer on. Gemini is the verifier (services/gemini.call_gemini).
from afc_ocr.services.local_ocr import get_engine
from afc_ocr.services.gemini import call_gemini
from afc_ocr.models import OCRTrainingPair, OCRCropLabel, _assign_split

import re
from rapidfuzz import fuzz

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_KILLS_RE = re.compile(r"(\d+)\s*elimin\w*", re.IGNORECASE)
# Fuzzy threshold to accept a detected box as "the same name" Gemini reported. Names are
# short + stylized, so we keep this moderate and let Gemini's spelling win on a match.
_MATCH_CUTOFF = 70.0


class Command(BaseCommand):
    help = "Bootstrap OCR training data from a folder of result screenshots (Gemini-verified)."

    def add_arguments(self, parser):
        parser.add_argument("--folder", required=True, help="Directory of result screenshots.")
        parser.add_argument("--event-type", default="team", choices=["team", "solo"],
                            help="How Gemini should read the screen (default team).")
        parser.add_argument("--limit", type=int, default=0, help="Only process the first N images (0 = all).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Recognize + Gemini-verify + align, but DO NOT write rows/crops.")

    def handle(self, *args, **opts):
        folder = opts["folder"]
        if not os.path.isdir(folder):
            raise CommandError(f"Folder not found: {folder}")
        if not getattr(settings, "GEMINI_API_KEY", None):
            raise CommandError(
                "No GEMINI_API_KEY set. Bootstrap needs Gemini to verify the names. The old key "
                "was leaked and revoked, generate a new one and put it in backend/.env."
            )

        images = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith(_IMG_EXTS)
        )
        if opts["limit"]:
            images = images[: opts["limit"]]
        if not images:
            raise CommandError(f"No images ({', '.join(_IMG_EXTS)}) in {folder}")

        engine = get_engine()
        event_type = opts["event_type"]
        dry = opts["dry_run"]

        totals = {"images": 0, "crops": 0, "disagreements": 0, "gemini_fail": 0}
        for name in images:
            path = os.path.join(folder, name)
            try:
                with open(path, "rb") as fh:
                    image_bytes = fh.read()
            except OSError as e:
                self.stderr.write(f"skip {name}: {e}")
                continue

            # 1. detector boxes (crop regions + candidate reads)
            boxes = engine._recognize(image_bytes)

            # 2. Gemini authoritative read (the verifier)
            try:
                gem = call_gemini(image_bytes, "image/jpeg", [], [])
            except Exception as e:
                totals["gemini_fail"] += 1
                self.stderr.write(f"Gemini failed on {name}: {e}")
                continue

            gem_names = self._gemini_names(gem)

            # 3. align detected NAME boxes to Gemini names
            crops, disagreements = self._align(boxes, gem_names, image_bytes)
            totals["images"] += 1
            totals["disagreements"] += len(disagreements)

            self.stdout.write(
                f"{name}: {len(boxes)} boxes, {len(gem_names)} gemini names, "
                f"{len(crops)} aligned crops, {len(disagreements)} unmatched"
            )

            if dry:
                continue

            # 4. persist the pair + crops
            self._persist(image_bytes, gem, crops, event_type)
            totals["crops"] += len(crops)

        self.stdout.write(self.style.SUCCESS(
            f"\nBootstrap done. images={totals['images']} crops={totals['crops']} "
            f"unmatched={totals['disagreements']} gemini_failures={totals['gemini_fail']}"
            + ("  (dry-run, nothing written)" if dry else "")
        ))
        if totals["disagreements"]:
            self.stdout.write(
                "Unmatched boxes/names are detector reads Gemini did not confirm (or vice "
                "versa). They were NOT saved as gold, review the source screenshots if the "
                "aligned-crop count looks low."
            )

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _gemini_names(gem: dict) -> list[str]:
        """Flatten Gemini's authoritative output to the list of player-name strings (the
        recognition truth we want the crops labeled with)."""
        out = []
        for p in (gem or {}).get("placements", []):
            for pl in p.get("players", []):
                nm = (pl.get("name") or "").strip()
                if nm:
                    out.append(nm)
        return out

    def _align(self, boxes, gem_names, image_bytes):
        """Match each detected NAME box to its Gemini name. Returns (crops, disagreements).
        crops = [{"crop_bytes", "text"}]; disagreements = the box texts that matched nothing."""
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        crops, disagreements = [], []
        used = set()
        for b in boxes:
            text = b["text"].strip()
            # skip kill-count boxes (a name box is what we train the recognizer on)
            if _KILLS_RE.fullmatch(text) or len(text.replace(" ", "")) < 2:
                continue
            name_part = _KILLS_RE.sub("", text).strip(" .:-")
            if len(name_part.replace(" ", "")) < 2:
                continue
            # best Gemini name for this box
            best, best_score = None, 0.0
            for i, gn in enumerate(gem_names):
                if i in used:
                    continue
                s = fuzz.ratio(name_part.lower(), gn.lower())
                if s > best_score:
                    best, best_score, best_i = gn, s, i
            if best is not None and best_score >= _MATCH_CUTOFF:
                used.add(best_i)
                crop_bytes = self._crop(img, b)
                if crop_bytes:
                    crops.append({"crop_bytes": crop_bytes, "text": best})
            else:
                disagreements.append(name_part)
        return crops, disagreements

    @staticmethod
    def _crop(img, box):
        """Crop the box region (with a small pad) and return PNG bytes, or None."""
        import cv2
        h, w = img.shape[:2]
        # box has cx, cy, h, x0; reconstruct a rough rect (the detector stored centre + height)
        bh = max(int(box["h"]), 8)
        bw = bh * max(len(box["text"]), 3) // 2  # rough width from char count
        x0 = max(int(box["x0"]) - 2, 0)
        y0 = max(int(box["cy"] - bh / 2) - 2, 0)
        x1 = min(x0 + bw + 4, w)
        y1 = min(int(box["cy"] + bh / 2) + 2, h)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = img[y0:y1, x0:x1]
        ok, buf = cv2.imencode(".png", crop)
        return buf.tobytes() if ok else None

    def _persist(self, image_bytes, gem, crops, event_type):
        """Write the OCRTrainingPair + one OCRCropLabel per aligned crop, content-addressing
        both the whole screenshot and each crop under media/ocr_training/."""
        sha = hashlib.sha256(image_bytes).hexdigest()
        img_rel = f"ocr_training/{sha}.jpg"
        if not default_storage.exists(img_rel):
            from django.core.files.base import ContentFile
            default_storage.save(img_rel, ContentFile(image_bytes))

        pair, created = OCRTrainingPair.objects.get_or_create(
            image_sha256=sha,
            defaults=dict(
                pair_id=uuid.uuid4(),
                event_type=event_type,
                raw_output=gem,
                final_json=gem,
                source="gemini_autolabel",
                teacher_model="gemini-2.5-pro",
                num_corrections=0,
                is_clean=True,
                split=_assign_split(sha),
                image_path=img_rel,
            ),
        )
        if not created:
            return  # already bootstrapped this exact image

        for c in crops:
            csha = hashlib.sha256(c["crop_bytes"]).hexdigest()
            crop_rel = f"ocr_training/crops/{csha}.png"
            if not default_storage.exists(crop_rel):
                from django.core.files.base import ContentFile
                default_storage.save(crop_rel, ContentFile(c["crop_bytes"]))
            OCRCropLabel.objects.create(
                crop_id=uuid.uuid4(),
                pair=pair,
                crop_path=crop_rel,
                field="name",
                text=c["text"],
                placement=0,
                matched_user_id=None,
            )
