#!/usr/bin/env python3
"""Which Django test suites a change has to run, split into parallel jobs (.github/workflows/tests.yml).

Owner, 14 Nov 2026 (AFC inbox #580 #581), after main stayed red for two days twice with nobody seeing it (CI ran no
Django tests): each pull request runs the tests of the parts of the server it touches, in parallel jobs; the whole
suite runs every night on main and opens an issue when it is red; an urgent fix can still merge (the job is not a
required check).

THE RULE. A changed file under afc_<app>/ selects that app AND every app whose code names it (an import or a string
like "afc_auth.User"), because a change to a model or a helper breaks its callers' tests, not only its own. A change
to the project itself (afc/ settings, urls, celery; requirements; manage.py; this file; the workflow) selects every
app. Anything else (docs, deploy scripts, tools that are not this one) selects nothing. Only apps with test files are
run. The selection is split into at most MAX_JOBS jobs, heaviest first, so the slowest job sets the wait.

Standard library only (it runs before anything is installed).

    python3 tools/test_plan.py --base origin/main            # a pull request
    python3 tools/test_plan.py --all                          # the nightly run
    python3 tools/test_plan.py --files afc_draws/views.py     # by hand
    python3 tools/test_plan.py --self-test
    ... --github-output "$GITHUB_OUTPUT"                      # writes groups=<json> and any=true|false
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_JOBS = 6
# A change here can break any app's tests.
RUN_ALL = ("afc/", "requirements", "manage.py", "tools/test_plan.py", ".github/workflows/tests.yml")


def all_apps(root=ROOT):
    """Every Django app of the project: a top-level afc_* folder with an apps.py."""
    return sorted(d for d in os.listdir(root)
                  if d.startswith("afc_") and os.path.isfile(os.path.join(root, d, "apps.py")))


def test_files(app, root=ROOT):
    """The app's test modules (test*.py / tests*.py at the top, or anything in a tests/ package)."""
    base = os.path.join(root, app)
    found = [f for f in os.listdir(base) if re.match(r"tests?.*\.py$", f)]
    pkg = os.path.join(base, "tests")
    if os.path.isdir(pkg):
        found += ["tests/" + f for f in os.listdir(pkg) if f.endswith(".py") and f != "__init__.py"]
    return sorted(found)


def _source(app, root=ROOT):
    """All of an app's Python source as one string (migrations left out: they name every app they depend on)."""
    chunks = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, app)):
        dirnames[:] = [d for d in dirnames if d not in ("migrations", "__pycache__")]
        for f in filenames:
            if f.endswith(".py"):
                try:
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        chunks.append(fh.read())
                except OSError:
                    continue
    return "\n".join(chunks)


def dependents(changed, apps, root=ROOT):
    """The apps whose source names any app in `changed` (the changed apps themselves included)."""
    pats = {a: re.compile(r"\b%s\b" % re.escape(a)) for a in changed}
    out = set(changed)
    for app in apps:
        if app in out:
            continue
        src = _source(app, root)
        if any(p.search(src) for p in pats.values()):
            out.add(app)
    return out


def select(files, root=ROOT):
    """The apps to test for a list of changed paths (repo-relative, forward slashes)."""
    apps = all_apps(root)
    files = [f.replace("\\", "/") for f in files if f.strip()]
    if any(f.startswith(RUN_ALL) for f in files):
        chosen = set(apps)
    else:
        changed = {f.split("/", 1)[0] for f in files if "/" in f and f.split("/", 1)[0] in apps}
        chosen = dependents(changed, apps, root) if changed else set()
    return sorted(a for a in chosen if test_files(a, root))


def split(apps, root=ROOT, max_jobs=MAX_JOBS):
    """Parallel jobs: heaviest app first into the lightest job (weight = number of test modules)."""
    weights = {a: len(test_files(a, root)) for a in apps}
    jobs = [[] for _ in range(min(max_jobs, len(apps)))]
    loads = [0] * len(jobs)
    for app in sorted(apps, key=lambda a: (-weights[a], a)):
        i = loads.index(min(loads))
        jobs[i].append(app)
        loads[i] += weights[app]
    return [{"name": job[0] if len(job) == 1 else "%s +%d" % (job[0], len(job) - 1), "apps": " ".join(job)}
            for job in jobs if job]


def changed_files(base):
    """Files a pull request changes: the diff from its merge base with `base`."""
    out = subprocess.run(["git", "diff", "--name-only", "%s...HEAD" % base], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def self_test():
    """The rule on a fixture tree: a leaf app, a shared app with a dependent, the project, a doc."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="test_plan_")

    def app(name, code="", tests=True):
        os.makedirs(os.path.join(tmp, name, "migrations"))
        open(os.path.join(tmp, name, "apps.py"), "w").close()
        with open(os.path.join(tmp, name, "views.py"), "w") as fh:
            fh.write(code)
        with open(os.path.join(tmp, name, "migrations", "0001_initial.py"), "w") as fh:
            fh.write("dependencies = [('afc_leaf', '0001')]")   # a migration naming an app is NOT a dependency
        if tests:
            open(os.path.join(tmp, name, "tests.py"), "w").close()

    app("afc_auth")
    app("afc_team", "from afc_auth.models import User\n")
    app("afc_leaf", "x = 1\n")
    app("afc_notests", "from afc_auth import models\n", tests=False)
    checks = [
        (["afc_leaf/views.py"], ["afc_leaf"]),
        (["afc_auth/models.py"], ["afc_auth", "afc_team"]),          # the dependent runs too; no-tests app skipped
        (["afc_team/views.py", "README.md"], ["afc_team"]),
        (["afc/settings.py"], ["afc_auth", "afc_leaf", "afc_team"]),
        (["requirements-prod.txt"], ["afc_auth", "afc_leaf", "afc_team"]),
        (["README.md", "deploy/vps/deploy-backend.sh"], []),
    ]
    bad = 0
    for files, want in checks:
        got = select(files, tmp)
        ok = got == want
        bad += not ok
        print("%s %s -> %s%s" % ("ok  " if ok else "FAIL", files, got, "" if ok else " (want %s)" % want))
    jobs = split(["afc_auth", "afc_team", "afc_leaf"], tmp, max_jobs=2)
    ok = sorted(a for j in jobs for a in j["apps"].split()) == ["afc_auth", "afc_leaf", "afc_team"] and len(jobs) == 2
    bad += not ok
    print("%s split into 2 jobs: %s" % ("ok  " if ok else "FAIL", jobs))
    print("self-test: %d failure(s)" % bad)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="pull request: diff against this ref (e.g. origin/main)")
    src.add_argument("--all", action="store_true", help="every app with tests (the nightly run)")
    src.add_argument("--files", nargs="+", help="these changed paths")
    src.add_argument("--self-test", action="store_true")
    ap.add_argument("--github-output", help="append groups=... and any=... to this file")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.all:
        apps = [a for a in all_apps() if test_files(a)]
    else:
        apps = select(changed_files(args.base) if args.base else args.files)
    groups = split(apps)
    print("apps to test (%d): %s" % (len(apps), " ".join(apps) or "none"))
    for g in groups:
        print("  job %-34s %s" % (g["name"], g["apps"]))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write("groups=%s\n" % json.dumps(groups))
            fh.write("any=%s\n" % ("true" if groups else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
