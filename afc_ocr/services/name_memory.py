"""
afc_ocr/services/name_memory.py
================================================================================
The OCR name memory: what each organization's reviewers taught the site about how player names
come out of a screenshot. One place that LEARNS and one that RECALLS, shared by both OCR flows.

Owner 2026-10-22 (inbox #334, #335): "each organizer gets its own ... memory of player names, once
an organizer is created and if they are deleted it gets removed", and "it should also learn from
each failed one". So:
  - every row a reviewer confirms teaches "this spelling is this player" (a confirmation);
  - every suggestion the reviewer did NOT keep (changed to another player, or marked "not on the
    platform") teaches "this spelling is NOT that player" (a rejection);
  - each organization has its own memory (afc_ocr.models.OCRNameAlias.organization, CASCADE);
    AFC-run events and leaderboards use organization=None.

RECALL rules (NameMemory.recall), applied by matching.match_name before any fuzzy matching:
  1. The same spelling (after normalize_search_text folds fancy fonts, case and symbols): the player
     with the most confirmations minus rejections, if that is above zero.
  2. Otherwise a CLOSE spelling (ratio >= CLOSE_READ, 0.92): only when every close remembered
     spelling points at ONE player, so two similar names on a roster are never confused.
  A recalled player is only used when they are on the roster being matched (the event roster, or
  the platform pool for standalone leaderboards); otherwise matching carries on as before.
  REJECTED (NameMemory.rejected): players turned down more often than confirmed for this exact
  spelling. match_name never auto-picks them for it; they stay in the candidate list.

LEARN (learn): called with (raw_name, suggested_user_id, chosen_user_id) decisions by
  - afc_ocr.views.commit_ocr_session (event OCR, after the results are written), and
  - afc_leaderboard.views._apply_ocr_rows (standalone leaderboards; the review sends each player's
    decision, see AFC_Frontend standalone OcrReviewTable).
Learning never blocks a commit: a failure is logged and the results stand.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.db.models import F

from utils.search_utils import normalize_search_text

logger = logging.getLogger(__name__)

# A remembered spelling counts as the same read at this similarity of the normalized forms. High on
# purpose: memory binds a player outright, so it must not reach across to a similar-looking name.
CLOSE_READ = 0.92
# Longest spelling kept (OCRNameAlias.raw_name / raw_norm are 100 characters).
_MAX = 100


def norm(raw_name) -> str:
    return normalize_search_text(raw_name or "")[:_MAX]


class NameMemory:
    """One organization's memory, loaded once per read (a screenshot is 30 to 50 names, so one query
    beats one per name). Build it with NameMemory(org) and hand it to matching.match_name(memory=)."""

    def __init__(self, organization):
        self.organization = organization
        self._by_norm = None   # {raw_norm: {user_id: net}}

    def _load(self):
        if self._by_norm is not None:
            return
        from afc_ocr.models import OCRNameAlias
        self._by_norm = {}
        rows = (OCRNameAlias.objects.filter(organization=self.organization, user__isnull=False)
                .values_list("raw_norm", "user_id", "match_count", "rejected_count"))
        for raw_norm, user_id, confirmed, rejected in rows:
            if raw_norm:
                self._by_norm.setdefault(raw_norm, {})[user_id] = confirmed - rejected

    def recall(self, raw_name):
        """(user_id, how) for a remembered player, how = "same" or "close"; (None, "") when nothing
        is remembered for this spelling or the memory disagrees with itself."""
        self._load()
        key = norm(raw_name)
        if not key or not self._by_norm:
            return None, ""
        same = self._by_norm.get(key)
        if same:
            user_id, net = max(same.items(), key=lambda kv: kv[1])
            if net > 0 and sum(1 for v in same.values() if v == net) == 1:
                return user_id, "same"
            return None, ""
        try:
            from rapidfuzz import fuzz
        except ImportError:  # the matcher degrades without rapidfuzz; so does memory
            return None, ""
        winners = set()
        for remembered, users in self._by_norm.items():
            if fuzz.ratio(key, remembered) / 100.0 >= CLOSE_READ:
                for user_id, net in users.items():
                    if net > 0:
                        winners.add(user_id)
        if len(winners) == 1:
            return winners.pop(), "close"
        return None, ""

    def rejected(self, raw_name) -> set:
        """Players turned down more often than confirmed for exactly this spelling."""
        self._load()
        return {uid for uid, net in (self._by_norm.get(norm(raw_name)) or {}).items() if net < 0}


def learn(organization, decisions) -> int:
    """Teach the memory from reviewed rows. `decisions` is an iterable of
    (raw_name, suggested_user_id, chosen_user_id); either id may be None. Returns how many facts were
    recorded. Never raises: a failure is logged and the reviewed results stand."""
    try:
        return _learn(organization, decisions)
    except Exception:  # noqa: BLE001 - learning must never undo a commit that already succeeded
        logger.exception("name_memory.learn failed for organization %s", getattr(organization, "pk", None))
        return 0


def _learn(organization, decisions) -> int:
    from afc_ocr.models import OCRNameAlias
    facts = 0
    for raw_name, suggested, chosen in decisions:
        key = norm(raw_name)
        if not key:
            continue
        shown = (raw_name or "").strip()[:_MAX]
        if chosen:
            _bump(OCRNameAlias, organization, key, shown, chosen, "match_count")
            facts += 1
        if suggested and suggested != chosen:
            _bump(OCRNameAlias, organization, key, shown, suggested, "rejected_count")
            facts += 1
    return facts


def _bump(model, organization, key, shown, user_id, counter):
    """Add one to `counter` on the (organization, spelling, player) row, creating it at 1. Retried once
    when two commits create the same row at the same moment (the unique constraint decides)."""
    for _ in range(2):
        updated = model.objects.filter(organization=organization, raw_norm=key, user_id=user_id).update(
            **{counter: F(counter) + 1})
        if updated:
            return
        try:
            with transaction.atomic():
                model.objects.create(organization=organization, raw_norm=key, raw_name=shown,
                                     user_id=user_id, **{counter: 1})
            return
        except IntegrityError:
            continue
