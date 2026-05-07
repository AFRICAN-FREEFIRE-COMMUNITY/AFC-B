"""Same as test_settings but does NOT disable migrations.

Used solely for `python manage_test.py makemigrations afc_auth` since the
default test_settings tells Django to skip afc_auth migrations entirely.
"""

from afc.test_settings import *  # noqa: F401,F403


# Re-enable migrations for all apps so makemigrations can write a real
# migration file for the new UserProfile fields.
class _Empty(dict):
    pass


MIGRATION_MODULES = _Empty()
