"""Shared pytest fixtures for afc_wallet tests."""

import os
import django

# Ensure tests run under the lightweight test settings even when invoked via
# `pytest` directly without DJANGO_SETTINGS_MODULE in env.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "afc.test_settings")
django.setup()

import pytest
from decimal import Decimal

from django.contrib.auth import get_user_model

from afc_wallet.models import FxSnapshot, Wallet
from afc_wallet.services import get_or_create_house_user


User = get_user_model()


@pytest.fixture
def fx_snapshot(db):
    return FxSnapshot.objects.create(
        ngn_per_usd=Decimal("1500.0000"), source="test"
    )


@pytest.fixture
def house_user(db):
    return get_or_create_house_user()


@pytest.fixture
def make_user(db):
    """Factory: returns a function that creates a fresh user + wallet."""

    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        n = counter["n"]
        defaults = dict(
            username=f"player_{n}",
            email=f"player_{n}@example.com",
            password="x",
            full_name=f"Player {n}",
            country="NG",
            role="player",
        )
        defaults.update(overrides)
        u = User.objects.create_user(**defaults)
        Wallet.objects.create(user=u)
        return u

    return _make


@pytest.fixture
def alice(make_user):
    return make_user(username="alice")


@pytest.fixture
def bob(make_user):
    return make_user(username="bob")
