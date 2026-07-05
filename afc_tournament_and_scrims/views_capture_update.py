"""
afc_tournament_and_scrims.views_capture_update — desktop AFC Capture FULL AUTO-UPDATE (owner 2026-07-05).

PURPOSE
    Installed copies of the AFC Capture tray client (afc-capture/) update THEMSELVES: on startup the
    client polls the version endpoint here, and when a newer version is published it downloads the new
    installer and runs it silently (a running .exe cannot overwrite its own file on Windows, so the
    installer does the file swap + relaunch). This module is the server side of that flow:

        GET  events/capture/version/    capture_version   — PUBLIC. The "what is the latest version"
                                                            descriptor the client polls. No token: it
                                                            exposes only a version string + a public
                                                            download URL + notes, nothing sensitive, and
                                                            the client has no upload token at startup.
        POST events/capture/releases/   capture_releases  — publish a new release (create a CaptureRelease,
                                                            mark it latest, clear the others). Gated to a
                                                            super admin (User.role == "admin") or a
                                                            head_admin via views._is_head_or_super_admin.
                                                            The owner publishes a new version by pasting the
                                                            hosted installer URL — NO code deploy to bump.

WHY A SEPARATE MODULE
    Same isolation rationale as views_capture_pending.py / event_links.py — keep the 19k-line views.py from
    growing, and keep this new auto-update surface next to the other capture endpoints (capture_resolve /
    capture_context live in views.py; capture_config lives in views_overlays.py).

    NOTE ON THE OLD capture_version: views_overlays.py has a legacy file-based capture_version (the earlier
    "thin launcher + payload zip" experiment reading MEDIA_ROOT/capture/capture_release.json). urls.py now
    routes capture/version/ to THIS model-based view instead. The launcher's payload path degrades safely
    (it reads a "version" key this response no longer sends, so it simply does nothing) — the installer
    auto-update below supersedes it. See afc-capture/README.md.

HOW IT CONNECTS
    - Model:    CaptureRelease (afc_tournament_and_scrims.models) — version / installer_url / notes /
                min_supported_version / is_latest / created_by / created_at.
    - Auth:     capture_version is PUBLIC; capture_releases uses afc_auth.views.validate_token (Bearer)
                + views._is_head_or_super_admin (super/head admin only).
    - Consumed by: the desktop client afc-capture/afc_capture/updater.py (fetch_latest_release parses the
                capture_version body; the admin publishes via capture_releases).
"""

from __future__ import annotations

from django.db import transaction

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import validate_token

from .models import CaptureRelease


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _serialize_release(rel: CaptureRelease) -> dict:
    """Shape one release for the PUBLIC version endpoint. Deliberately exposes ONLY non-sensitive fields
    (a version string, a public download URL, notes) — no tokens, no user data — because this is served
    without auth to a client that may not hold an upload token yet at startup."""
    return {
        "latest_version": rel.version,
        "installer_url": rel.installer_url,
        "notes": rel.notes or "",
        # Optional advisory floor; "" when unset. The client may treat being below it as a required update.
        "min_supported_version": rel.min_supported_version or "",
        "created_at": rel.created_at.isoformat() if rel.created_at else None,
    }


# --------------------------------------------------------------------------- #
# GET events/capture/version/  — PUBLIC latest-release descriptor
# --------------------------------------------------------------------------- #
@api_view(["GET"])
def capture_version(request):
    """GET events/capture/version/ — the latest published desktop AFC Capture release, PUBLIC (no token).

    Request:  no params, no auth.
    Response 200:
        {"latest_version": "1.3.0", "installer_url": "https://.../AFC-Capture-Setup.exe",
         "notes": "...", "min_supported_version": "", "created_at": "..."}
        or, when nothing has been published yet, the same shape with latest_version = null.

    The desktop client (afc-capture/afc_capture/updater.py fetch_latest_release) compares latest_version
    to its own afc_capture.__version__ using numeric semver comparison and, if newer, downloads +
    silently runs installer_url. PUBLIC on purpose: the client has no upload token at startup, and the
    body carries nothing sensitive (only a version + a public download URL)."""
    rel = CaptureRelease.objects.filter(is_latest=True).order_by("-created_at").first()
    if rel is None:
        # No release published yet — return a well-formed body (200) so the client can cheaply treat a
        # null latest_version as "nothing to update to" without special-casing a 404.
        return Response({
            "latest_version": None,
            "installer_url": "",
            "notes": "",
            "min_supported_version": "",
            "created_at": None,
        }, status=status.HTTP_200_OK)
    return Response(_serialize_release(rel), status=status.HTTP_200_OK)


# --------------------------------------------------------------------------- #
# POST events/capture/releases/  — publish a new release (admin only)
# --------------------------------------------------------------------------- #
@api_view(["POST"])
def capture_releases(request):
    """POST events/capture/releases/ — publish a new desktop AFC Capture release. Super/head admin only.

    Auth:     Authorization: Bearer <session token>; the user must be a super admin (User.role == "admin")
              or carry the head_admin granular role (views._is_head_or_super_admin). NOT event_admins,
              NOT organizers — publishing an app update is a platform-wide action.
    Request body (JSON):
        {"version": "1.3.0",                              # required, semver
         "installer_url": "https://.../AFC-Capture-Setup.exe",  # required, where the installer is hosted
         "notes": "optional changelog",                   # optional
         "min_supported_version": "1.0.0"}                # optional advisory update floor
    Response 201: {"message": "...", "release": {serialized}}  — is_latest set on this row, cleared on the rest.

    HOW THE OWNER USES IT: build the new installer (Inno Setup AFC-Capture.iss), upload it to any static
    host, then POST here with the version + that URL. Installed clients pick it up on their next launch.
    Reads/writes: CaptureRelease. Called by the admin (curl / a future tiny admin UI); no frontend built
    yet (documented in afc-capture/README.md)."""
    # ── Auth: Bearer session -> super/head admin gate (mirrors views_capture_pending._pending_gate) ──
    # _is_head_or_super_admin is imported lazily to avoid a load-time circular import with the big views.py.
    from .views import _is_head_or_super_admin

    auth = request.headers.get("Authorization") or ""
    user = validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") else None
    if not user:
        return Response({"message": "Invalid or expired session token."},
                        status=status.HTTP_401_UNAUTHORIZED)
    if not _is_head_or_super_admin(user):
        return Response({"message": "Only a super admin or head admin may publish a capture release."},
                        status=status.HTTP_403_FORBIDDEN)

    # ── Validate the payload (minimal: version + installer_url are the load-bearing fields) ──
    data = request.data if isinstance(request.data, dict) else {}
    version = str(data.get("version") or "").strip()
    installer_url = str(data.get("installer_url") or "").strip()
    notes = str(data.get("notes") or "").strip()
    min_supported = str(data.get("min_supported_version") or "").strip()

    if not version:
        return Response({"message": "version is required (e.g. \"1.3.0\")."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not installer_url:
        return Response({"message": "installer_url is required (where the installer .exe is hosted)."},
                        status=status.HTTP_400_BAD_REQUEST)
    # Guard against a fat-fingered URL (the client will try to download this). Accept only http(s).
    if not (installer_url.startswith("http://") or installer_url.startswith("https://")):
        return Response({"message": "installer_url must be an http(s) URL."},
                        status=status.HTTP_400_BAD_REQUEST)

    # ── Publish atomically: create the row, make it the ONLY latest ──
    # transaction.atomic so we never leave two rows flagged is_latest (the version endpoint picks the
    # newest latest anyway, but keeping exactly one keeps the state clean + auditable).
    with transaction.atomic():
        CaptureRelease.objects.filter(is_latest=True).update(is_latest=False)
        rel = CaptureRelease.objects.create(
            version=version,
            installer_url=installer_url,
            notes=notes,
            min_supported_version=min_supported,
            is_latest=True,
            created_by=user,
        )

    return Response({
        "message": f"Published AFC Capture v{version} as the latest release.",
        "release": _serialize_release(rel),
    }, status=status.HTTP_201_CREATED)
