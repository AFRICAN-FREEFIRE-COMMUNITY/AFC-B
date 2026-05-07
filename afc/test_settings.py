"""Lightweight test settings for afc_wallet + afc_wager.

The full `afc.settings` pulls a giant dependency tree (pandas, easyocr,
tesseract, paystack, etc.) and the existing afc_shop/afc_tournament_and_scrims
views have pre-existing Python 3.11 syntax issues. To run the wallet/wager
test suite in isolation, this settings module:

- Uses sqlite in-memory DB (fast, parallel-safe).
- Loads only the apps needed for our work: afc_auth (custom user model
  is required for AUTH_USER_MODEL), afc_team + afc_tournament_and_scrims
  (afc_auth.User imports tournament Stages/StageGroups), afc_wallet,
  afc_wager.
- Avoids importing afc_shop/afc_player/afc_ocr — those have unrelated
  bugs that block running ANY test in the project today.

Running:

    DJANGO_SETTINGS_MODULE=afc.test_settings python manage.py test \
        afc_wallet afc_wager
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "test-only-secret-key"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # afc_auth must come BEFORE the apps it forward-references via FK strings.
    # The legacy apps have circular imports at module level (afc_auth ->
    # afc_tournament_and_scrims -> afc_team -> afc_auth) — Django handles
    # FK string lazy-resolution, but the existing modules `import` from each
    # other directly. We work around this by importing the deepest module
    # first via a shim below.
    "afc_auth",
    "afc_team",
    "afc_tournament_and_scrims",
    # New wager + wallet apps:
    "afc_wallet",
    "afc_wager",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "afc.test_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # File-backed DB so `manage_test.py migrate` (CLI invocation, single
        # process) can be run separately for sanity-checking. Test runner
        # uses an in-memory clone via TEST setting below.
        "NAME": str(BASE_DIR / "test_db.sqlite3"),
        # 60-second busy timeout — concurrency tests use threading + sqlite,
        # which short-circuits to a busy lock under contention. Without
        # this, tests flake on slower CI runners.
        "OPTIONS": {"timeout": 60},
        "TEST": {
            "NAME": ":memory:",
        },
    }
}

AUTH_USER_MODEL = "afc_auth.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Disable migrations from afc_team / afc_tournament_and_scrims / afc_auth
# during tests — Django uses CREATE TABLE from models.py via syncdb. This
# avoids importing the project's many existing migrations (some of which
# reference removed apps like afc_shop).


class DisableExistingMigrations(dict):
    """When `MIGRATION_MODULES[app] = None`, Django skips migrations for that
    app and creates tables straight from the models.py.

    We disable migrations for the legacy apps so they don't try to import
    afc_shop or afc_player. Our new apps (afc_wallet, afc_wager) keep their
    migrations so we exercise the migration path the way prod will."""

    LEGACY_APPS = {
        "afc_auth",
        "afc_team",
        "afc_tournament_and_scrims",
        "auth",
        "contenttypes",
        "admin",
        "sessions",
    }

    def __contains__(self, key):
        return key in self.LEGACY_APPS

    def __getitem__(self, key):
        return None


MIGRATION_MODULES = DisableExistingMigrations()

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"

# HMAC signing key for AdminAuditLog (test-only)
WALLET_AUDIT_HMAC_SECRET = os.environ.get(
    "WALLET_AUDIT_HMAC_SECRET", "test-audit-secret"
)
