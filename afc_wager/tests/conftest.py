"""Shared pytest fixtures for afc_wager tests."""

import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.test_settings")
django.setup()
