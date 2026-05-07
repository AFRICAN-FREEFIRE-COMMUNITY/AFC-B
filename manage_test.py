#!/usr/bin/env python
"""Test-only entrypoint for the wager/wallet apps.

Uses `afc.test_settings` (sqlite in-memory, only the apps we need) so the
suite runs without the existing project's heavy/broken dependencies.

Usage:

    python manage_test.py test afc_wallet afc_wager
    python manage_test.py makemigrations afc_wallet
"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.test_settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Activate the virtualenv first."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
