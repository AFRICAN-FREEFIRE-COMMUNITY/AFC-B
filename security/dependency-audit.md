# Dependency audit (owner rule R80)

`pip-audit` is not on PATH on the Windows workstation, so the checker cannot run it for itself and
reports "pip-audit gave no JSON". It IS installed in the backend virtualenv, and this file records
what it said, so the claim can be checked rather than believed.

## How to run it

    backend/.venv/Scripts/python.exe -m pip install pip-audit      # once
    backend/.venv/Scripts/python.exe -m pip_audit -r requirements-prod.txt

On the VPS the same thing, inside the server's venv:

    cd /home/ubuntu/AFC-B && venv/bin/python -m pip_audit -r requirements-prod.txt

## 2026-09-22, the first run

99 known advisories across five packages. Fixed by pinning each to the first release on its own
line that carries the fix, so nothing jumped a major version:

| Package | Was | Now | Advisories |
|---|---|---|---|
| Django | 5.2.7 | 5.2.17 | 57 |
| djangorestframework | 3.17.1 | 3.17.2 | 2 |
| pillow | 12.2.0 | 12.3.0 | 25 |
| pyasn1 | 0.6.3 | 0.6.4 | 6 |
| sqlparse | 0.5.5 | 0.6.0 | 9 |

Re-run after the pins: **No known vulnerabilities found.**

Proven on the upgraded stack the same day: `manage.py check` clean, and 317 tests green
(afc_team, afc_organizers, and the WhatsApp, bot-protection, api-errors and image-gate suites).
Pillow matters twice over here, because it IS the upload gate: every image door decodes through it
(afc_auth/image_utils.require_image_upload).

## Next

Run it on the box after the next deploy, and record the date here. A count that has not been
re-checked in 90 days should be treated as unknown, not as zero.
