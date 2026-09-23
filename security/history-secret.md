# Secrets that were in this repository's history (owner rule R62)

Rewritten out on **2026-09-23** with the owner's approval ("go"). This file is the record of what
was in there, what was done, and what still needs a human at a provider console.

**It names no secret values.** Each row says where a value lived and what it was, which is enough to
find it at the provider.

## What history held: eight AFC-owned secrets, not one

GitGuardian flagged one Google app password on 2026-09-22. Listing the rest found seven more.

| What | Where it lived | Deployed today? | Rotated? |
|---|---|---|---|
| `MINTROUTE_SECRET_KEY` | `afc/settings.py`, `afc_shop/services/mintroute.py` | yes, and it matches the box's `.env` | **sandbox key** (owner, 2026-09-23), so it signs nothing that spends money. Replace when the live key is issued |
| Google app password, `africanfreefirecommunity3@gmail.com` | `afc_auth/views.py`, commented mail block | no | revoked 2026-09-22 |
| Three MORE Google app passwords, earlier copies of that block | `afc_auth/views.py` (`77a9f7d9`, `dab98ccb`, `5a1c9576`) | no | **NOT revoked: nobody knew they existed.** Owner action |
| SMTP password, 16 characters | `afc_auth/views.py`, beside `from_address` | no | owner action |
| Google API key (`AIza...`) | `ocr_local_app.py`, `ocr_test.py` | no | owner action |
| Django `SECRET_KEY` | `afc/settings.py` | no, production reads its own from `.env` | not urgent; rotating signs every user out |

Whether each is deployed was settled by hashing every value in `/home/ubuntu/AFC-B/.env` and
comparing against SHA-256 of the eight. Nothing secret crossed the wire in either direction. The
owner-facing list is `WEBSITE/ROTATE-THESE-2026-09-23.md`.

**Left in place on purpose:** nine key-shaped values inside vendored `venv/` and `eb-env/` files
(botocore, allauth, httpx test data, from when the virtualenv was committed) are library test data,
not AFC's. Four values are in the CURRENT tree and a history rewrite must never edit live code:
three test fixtures, and a database password in tracked `PRODUCTION_DB_FIX.md`, which deserves its
own fix because that is a secret in the repository, not merely in its history.

## What was done

Two `git filter-repo` passes on a mirror clone, then a force-push of all 25 branches:

1. `--replace-text` with the eight values.
2. `--invert-paths --path-glob '*__pycache__*' --path-glob '*.pyc' --prune-empty never`, because
   three of the eight survived pass 1 inside committed compiled blobs. No branch tip tracks a
   `.pyc`, so this changed no live code. `--prune-empty never` because the first attempt silently
   dropped a commit the path pass had emptied (main went 1454 to 1453).

Verified before pushing, and again afterwards from a **fresh clone of GitHub**:

- all eight values gone, and still present in the untouched backup mirror, so the check has teeth
- 25 branches in, 25 branches out; every commit count identical; main still 1454 commits
- **main's tip tree byte-identical at `b447f346`**, which is the proof that live code was untouched
- `check-security --rule R62` on the fresh clone: **0 high, 0 medium**

AFC-B has no tags, so nothing else pinned the old commits.

## What this does NOT do

- **It does not end an exposure.** A string that has been on GitHub is a string somebody may hold.
  Only rotating at the provider ends it, which is why the table above tracks rotation separately.
- **GitHub keeps unreachable objects fetchable by SHA for a while** after a force-push, and an open
  PR pins the commits it references. Ask GitHub Support to garbage-collect if the old SHAs must be
  unreachable immediately.
- **A clone made before 2026-09-23 still has everything.** Anyone holding one re-clones. On this
  machine the worktrees were re-pointed the same day, except `backend/`, which sits on
  `feat/fantasy-league` with uncommitted work and was deliberately left for the owner to rebase.

## If R62 ever reads more than 0 again

On a fresh clone it reads 0. A local checkout that still has pre-rewrite branches will report them,
because the checker reads `git log --all`, and that includes stale local refs. That is local
hygiene, not a finding: prune the branches whose upstream is `[gone]`, or re-clone.
