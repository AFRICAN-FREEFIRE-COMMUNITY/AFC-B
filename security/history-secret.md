# The Gmail app password in this repository's history (owner rule R62)

Written 2026-09-23. **Read this before touching the R62 line in `security/debt.json`.**

## What it is

One Google app password for `africanfreefirecommunity3@gmail.com`, in a commented-out block at the
top of `afc_auth/views.py`. Never live code: the site's mail goes through Office 365
(`EMAIL_HOST` defaults to smtp.office365.com and the box's `.env` carries only `EMAIL_PASSWORD`).
It was still a working door into that mailbox, for sending AND for IMAP, until it was revoked.

**It has been revoked at Google by the owner on 2026-09-22.** That, and only that, ended the
exposure. Everything below is about tidying up after it.

## Where it is

| Commit | Date | Author | What it did |
|---|---|---|---|
| `1079d035` | 2025-11-23 | HabeebFF | added it, in a commented block, message "push it" |
| `e6925698` | 2026-03-30 | HabeebFF | moved it within the same file, so the line was removed and re-added |
| `21797e9e` | 2026-09-23 | this work | deleted the whole dead block |
| `e46e2204` | 2026-09-23 | squash merge of #86 | carries that deletion onto `main` |

Four commits, ONE credential. The scan reports a commit whenever a key-shaped line is added OR
removed, so a removal shows up exactly like an addition: `git log -G` cannot tell the difference,
and it should not try to.

GitGuardian found it on 2026-09-22 at 22:44:30 UTC, not because it was new, but because the
refusal-codes pass edited `afc_auth/views.py` and a file you touch is a file you republish.

## Why the checker missed it for ten months

The generic rule wanted a long opaque value with no spaces. A Google app password is four
four-letter words. Both the file scan and the history scan carry that shape now
(`[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}` within 60 characters of a gmail address), self-test 34/34.

## Why R62's ledger says 4 and not 0

The working tree is clean; history is not, and it cannot be cleaned without
`git filter-repo --replace-text` plus a **force-push to a protected branch**, which invalidates
every clone and every open branch. That is the owner's decision, not a checker's.

Leaving R62 at 0 in the ledger blocks every commit in BOTH repos, because the frontend's
`check-all` runs this ledger too and a rise in HIGH fails the run. So the ceiling records the four
commits, and this file is the reason a reader is owed. It is not "accepted debt" in the normal
sense: the credential is dead, so the four HIGHs describe a string that no longer opens anything.

**When the history is rewritten, the count drops to 0 by itself.** Re-run
`check-security --baseline` that day, and delete this section rather than editing the number.

## If the owner says go

1. Confirm again that the password is revoked (it is; re-check at myaccount.google.com if in doubt).
2. `git filter-repo --replace-text replacements.txt` with the literal old value on the left.
   Keep `replacements.txt` OUT of the repo: it holds the secret by definition.
3. Force-push `main` with branch protection lifted for the push, then restore protection.
4. Everybody re-clones. Any open branch is rebased onto the rewritten history or abandoned.
5. `check-security --baseline`, and R62 returns to 0.
