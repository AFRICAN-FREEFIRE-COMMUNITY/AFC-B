"""
CONNECTED ACCOUNTS: the outside accounts a player links to their AFC account.

This package is the INBOUND direction of identity. It is not to be confused with afc_sso, which is
the OUTBOUND direction ("Sign in with AFC", where AFC is the identity provider and a partner org is
the consumer). Both are surfaced on the same page in the player's profile, in two sections.

Import from here, not from the submodules, so the public surface stays small:

    from afc_auth.connections import get_provider, enabled_providers, is_enabled
"""
from .registry import Provider, enabled_providers, get_provider, is_enabled

__all__ = ["enabled_providers", "get_provider", "is_enabled", "Provider"]
