"""
tools/run_tests.py - run the Django suite so the result can be TRUSTED.

WHY THIS EXISTS
    On 2026-08-21 a release was nearly judged on two suite runs that reported 22 and then 46
    errors. Every single one was `(1213, 'Deadlock found when trying to get lock')`. No code was
    broken. Two suites were running at the same time against the same test database, because a
    second one was started while the first was still going, and the worktree was edited underneath
    both of them.

    That is worse than a red run: the failures LOOK like real defects, they land in files nobody
    re-reads, and the honest response ("re-run it") costs an hour each time. A resolution not to do
    it again is not a fix, because the mistake is invisible while you are making it. This script
    makes both halves impossible to do by accident:

      1. AN EXCLUSIVE LOCK. A second run refuses to start and names the process already holding it,
         instead of quietly deadlocking against it.
      2. A WORKTREE FINGERPRINT. The tracked files are hashed before and after. If anything changed
         while the suite ran, the result is declared UNTRUSTWORTHY and the exit code is non-zero,
         however green the tests looked, because the code that ran is not the code on disk.

    Neither check can be satisfied by being careful. That is the point.

USAGE
    backend/.venv/Scripts/python.exe tools/run_tests.py [<label>] [-- <manage.py test args>]

    tools/run_tests.py                          # whole suite
    tools/run_tests.py -- afc_results_import    # one app
    tools/run_tests.py ship -- --failfast       # labelled, for a release run

    Always adds --keepdb --noinput: --noinput because a prompt against a leftover test database
    turns an unattended run into an EOFError, and --keepdb because rebuilding costs minutes.

CONNECTS TO
    Nothing in the app. A developer tool, run by hand or by an agent doing a release check. Lives in
    this repo for the same reason tools/shoot.py does: it runs on this repo's virtualenv, and a tool
    that guards a release must not be the one thing that is untracked.
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOCK = os.path.join(REPO, ".test-run.lock")


def _fingerprint():
    """One hash over every tracked file's content, plus the HEAD commit.

    git ls-files rather than a directory walk, so .pyc, logs, the venv and scratch files cannot
    make an untouched worktree look changed.
    """
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        listing = subprocess.run(["git", "ls-files", "-s"], cwd=REPO,
                                 capture_output=True, text=True).stdout
        # -s gives the staged blob hash, which does not notice an UNCOMMITTED edit, so the working
        # copy of every tracked file is hashed as well. That is the case that actually bit: files
        # edited mid-run and never committed until afterwards.
        dirty = subprocess.run(["git", "diff", "--no-color"], cwd=REPO,
                               capture_output=True, text=True).stdout
        blob = (head + listing + dirty).encode("utf-8", "replace")
        return hashlib.sha256(blob).hexdigest()
    except Exception as exc:            # not a git repo, git missing - degrade, do not block
        print(f"   !! could not fingerprint the worktree ({exc}); the change check is OFF")
        return None


def _stale_lock_owner():
    """The pid recorded in an existing lock, or None when the lock is absent or its owner is gone."""
    if not os.path.exists(LOCK):
        return None
    try:
        with open(LOCK, encoding="utf-8") as fh:
            pid = int((fh.read().split("\n", 1)[0] or "0").strip())
    except Exception:
        return None
    if pid <= 0:
        return None
    # Windows: tasklist is the portable-enough check without adding a dependency.
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True).stdout
        return pid if str(pid) in out else None
    except Exception:
        return pid          # cannot tell - assume it is alive and refuse, which is the safe way


def main(argv):
    args = argv[1:]
    label = ""
    if args and not args[0].startswith("-"):
        label, args = args[0], args[1:]
    if args and args[0] == "--":
        args = args[1:]

    owner = _stale_lock_owner()
    if owner:
        print(f"REFUSING: a suite is already running in this repo (pid {owner}).")
        print("Two suites against one test database deadlock each other, and the failures look")
        print("like real defects. Wait for it, or stop it, then run again.")
        return 2
    if os.path.exists(LOCK):
        print("   (clearing a lock whose owner is gone)")
        os.remove(LOCK)

    with open(LOCK, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n{label}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    before = _fingerprint()
    started = time.time()
    try:
        cmd = [sys.executable, "manage.py", "test", "--keepdb", "--noinput"] + args
        print(f"RUN: {' '.join(cmd)}")
        rc = subprocess.run(cmd, cwd=REPO).returncode
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass

    after = _fingerprint()
    mins = (time.time() - started) / 60
    # ASCII only. The Windows console is cp1252, and a box-drawing character here crashed the very
    # run this script exists to protect.
    print(f"\n-- suite finished in {mins:.1f} min, exit {rc}")

    if before and after and before != after:
        print("UNTRUSTWORTHY: tracked files changed while the suite was running.")
        print("The code that ran is not the code on disk, so this result proves nothing about")
        print("either. Re-run it without editing the worktree.")
        return 3
    if rc == 0:
        print("TRUSTWORTHY: nothing else was running, and nothing changed underneath it.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
