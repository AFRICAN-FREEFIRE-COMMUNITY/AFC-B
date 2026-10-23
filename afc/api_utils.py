"""
afc.api_utils - shared request helpers for the function-based DRF views across the project.

Cleanup 2026-06-14: the Bearer-token auth handshake was copy-pasted as a private `_authenticate`
helper across several afc_organizers/afc_player_market view modules (and inlined hundreds of times
in the big monolith view files). This module holds the ONE canonical version so the modules that had
an identical copy import it instead of re-declaring it.

`authenticate` mirrors validate_token's contract exactly (401 / 401 / 401 since 2026-09-30; was 400/400/401 like the existing
copies returned), so swapping a module's local copy for this import is behaviour-preserving. The
divergent copies (e.g. afc_player_market.views_moderation, which returns different messages, and the
inline blocks in the monolith views) are intentionally left untouched.
"""
from rest_framework.response import Response
from rest_framework import status

# validate_token lives centrally in afc_auth.views (the same import every view module already uses);
# importing it here does not create a cycle (afc_auth.views never imports this module).
from afc_auth.views import validate_token


def authenticate(request):
    """Standard Bearer + validate_token handshake. Returns (user, error_response): exactly one is
    non-None. 401 in every case where there is no usable session: a missing header, one that is not
    "Bearer <token>" (an empty token arrives as "Bearer", trimmed), or a token that resolves to no
    live session. It was 400 for the first two until 2026-09-30 (inbox #104): the site only offers
    "sign in again" on a 401, so a lapsed session on the create-team page read "Invalid token
    format" and could not recover."""
    session_token = request.headers.get("Authorization")
    if not session_token:
        return None, Response(
            {"message": "Please sign in to continue.", "code": "authorization_header_required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Please sign in to continue.", "code": "invalid_token_format"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    user = validate_token(session_token.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token.", "code": "invalid_expired_session_token"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None
