"""
afc_organizers/views_ai_key.py - the organization's own AI key for OCR (owner 2026-09-12).

Routes (afc_organizers/urls.py, mounted at organizers/):
    GET    organization/<slug>/ai-key/            the connected key (never the key: provider, model,
                                                  last four, who and when, last test) + the provider
                                                  list + the free reads left
    PUT    organization/<slug>/ai-key/            {provider, model?, base_url?, key} -> tests the key
                                                  on a built-in sample screenshot FIRST; saves only
                                                  when it works; answers the test verdict
    DELETE organization/<slug>/ai-key/            disconnect
    POST   organization/<slug>/ai-key/test/       {key?, provider?, model?, base_url?} test a pasted
                                                  key (before saving) or the saved one (no body);
                                                  5 per 10 minutes per organization
    GET    organization/<slug>/ai-key/usage/      reads this month / all time / last, estimated spend
    GET    organization/<slug>/ai-key/history/    who connected / changed / tested / disconnected
    GET    admin/ai-keys/                         AFC staff: every org's provider, reads, allowance
    PUT    admin/ai-keys/<org_id>/allowance/      {free_reads_left}
    PUT    admin/ai-keys/<org_id>/ocr-disabled/   {disabled: bool}

Gate: the organization's owner, or an active member with can_manage_members (the "manage the
organization" member); event helpers never reach it. AFC platform admins pass everywhere.
The key is sealed with afc_auth.secret_box, and this module returns last_four only, ever.

Consumed by the frontend: app/(organizer)/organizer/settings/ai-key/page.tsx (the connect page
with the plain-text guides) and app/(a)/a/ocr/keys/page.tsx (the admin page), through
lib/api/aiKey.ts.
"""
import io
import logging
import time

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_ocr.services.providers import Credentials, ProviderError, read_with, registry
from afc_ocr.services.gemini import build_prompt

from .models import Organization, OrganizationAiKey, OrganizationAiKeyEvent, OrganizationMember
from .permissions import is_platform_org_admin

logger = logging.getLogger(__name__)

TESTS_PER_WINDOW = 5
TEST_WINDOW_SECONDS = 600


# ── auth + gate ─────────────────────────────────────────────────────────────────────────────
def _auth(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Authorization header is required"}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def may_manage_key(user, org) -> bool:
    """The owner, a member with can_manage_members, or an AFC platform admin."""
    if is_platform_org_admin(user):
        return True
    return OrganizationMember.objects.filter(
        organization=org, user=user, status="active",
    ).filter(Q(role="owner") | Q(can_manage_members=True)).exists()


def _org_and_gate(request, slug):
    user, err = _auth(request)
    if err:
        return None, None, err
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return None, None, Response({"message": "Organization not found."}, status=404)
    if not may_manage_key(user, org):
        return None, None, Response(
            {"message": "Only the organization's owner or a member who manages the organization can change its AI key."},
            status=403)
    return user, org, None


def _log(org, actor, action, provider="", last_four="", detail=""):
    OrganizationAiKeyEvent.objects.create(
        organization=org, actor=actor, action=action, provider=provider, last_four=last_four, detail=detail[:300],
    )


# ── the built-in sample the Test button reads ───────────────────────────────────────────────
_SAMPLE = None


def sample_screenshot() -> bytes:
    """A small, plain results table rendered on the fly: three teams, placement, kills. Enough for
    any vision model to read, no real player in it, and no file to ship. Cached per process."""
    global _SAMPLE
    if _SAMPLE is None:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (640, 300), (18, 20, 24))
        d = ImageDraw.Draw(img)
        try:
            from afc_ocr.services.synth import _candidate_font_paths
            font_path = next((p for p in _candidate_font_paths() if p), None)
            big = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
            small = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
        except Exception:  # noqa: BLE001 - any font renders a readable sample
            big = small = ImageFont.load_default()
        d.text((30, 20), "MATCH RESULTS", fill=(255, 200, 60), font=big)
        d.text((30, 80), "RANK   TEAM             KILLS", fill=(180, 180, 190), font=small)
        rows = [("#1", "AFC TEST ONE", "9"), ("#2", "AFC TEST TWO", "6"), ("#3", "AFC TEST THREE", "4")]
        for i, (rank, team, kills) in enumerate(rows):
            y = 130 + i * 48
            d.text((30, y), rank, fill=(255, 255, 255), font=small)
            d.text((130, y), team, fill=(255, 255, 255), font=small)
            d.text((430, y), kills, fill=(255, 255, 255), font=small)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _SAMPLE = buf.getvalue()
    return _SAMPLE


def run_test(provider, model, base_url, api_key):
    """One read of the sample on the given credentials. Returns (ok, message, rows, ms)."""
    try:
        entry = registry.get(provider)
    except KeyError:
        return False, "Unknown provider.", 0, 0
    if not api_key:
        return False, "Paste a key first.", 0, 0
    model = model or entry.get("recommended_model", "")
    if not model:
        return False, "Pick a model id first.", 0, 0
    if entry["id"] == "custom" and not base_url:
        return False, "This provider needs a base URL (it usually ends in /v1).", 0, 0
    creds = Credentials(provider=provider, model=model, api_key=api_key, base_url=base_url or "", paid_by="org")
    prompt = build_prompt([], [], prompt_kind="team_standings")
    started = time.monotonic()
    try:
        out = read_with(creds, sample_screenshot(), "image/png", prompt, aliases=[], team_notes=[],
                        prompt_kind="team_standings")
    except ProviderError as exc:
        return False, exc.message, 0, int((time.monotonic() - started) * 1000)
    except Exception:  # noqa: BLE001 - never leak internals; the log has them
        logger.exception("AI key test failed unexpectedly")
        return False, "The test failed for a reason this page cannot explain. Try again in a minute.", 0, int((time.monotonic() - started) * 1000)
    ms = int((time.monotonic() - started) * 1000)
    rows = len(out.get("placements") or [])
    if rows == 0:
        return False, "The provider answered but read no rows from the sample. Try the recommended model.", 0, ms
    return True, f"Works. Read {rows} rows from the sample in {ms / 1000:.1f} s on {model}.", rows, ms


def _rate_limited(org) -> bool:
    k = f"ai-key-test:{org.pk}"
    n = cache.get(k, 0)
    if n >= TESTS_PER_WINDOW:
        return True
    cache.set(k, n + 1, TEST_WINDOW_SECONDS)
    return False


# ── serializers ─────────────────────────────────────────────────────────────────────────────
def _key_payload(key):
    if key is None:
        return None
    return {
        "provider": key.provider,
        "provider_name": registry.BY_ID.get(key.provider, {}).get("name", key.provider),
        "model": key.model,
        "base_url": key.base_url,
        "last_four": key.last_four,
        "added_by": getattr(key.added_by, "username", None),
        "added_at": key.added_at.isoformat() if key.added_at else None,
        "last_tested_at": key.last_tested_at.isoformat() if key.last_tested_at else None,
        "last_test_ok": key.last_test_ok,
        "last_error": key.last_error,
        "consecutive_failures": key.consecutive_failures,
    }


def _usage_payload(org):
    from afc_ocr.models import OcrUsage
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    qs = OcrUsage.objects.filter(organization=org)
    month = qs.filter(created_at__gte=month_start)
    agg_m = month.aggregate(n=Count("id"), ok=Count("id", filter=Q(ok=True)), usd=Sum("cost_estimate_usd"))
    agg_all = qs.aggregate(n=Count("id"), ok=Count("id", filter=Q(ok=True)), usd=Sum("cost_estimate_usd"))
    last = qs.order_by("-created_at").first()
    return {
        "month": {"reads": agg_m["n"], "ok": agg_m["ok"], "estimated_usd": float(agg_m["usd"] or 0)},
        "all_time": {"reads": agg_all["n"], "ok": agg_all["ok"], "estimated_usd": float(agg_all["usd"] or 0)},
        "last_read_at": last.created_at.isoformat() if last else None,
        "last_read_ok": last.ok if last else None,
        "last_read_error": last.error if last else "",
        "free_reads_left": org.ocr_free_reads_left,
        "ocr_disabled": org.ocr_disabled,
    }


# ── organizer endpoints ─────────────────────────────────────────────────────────────────────
@api_view(["GET", "PUT", "DELETE"])
def ai_key(request, slug):
    user, org, err = _org_and_gate(request, slug)
    if err:
        return err
    key = OrganizationAiKey.objects.filter(organization=org).select_related("added_by").first()

    if request.method == "GET":
        return Response({
            "key": _key_payload(key),
            "providers": registry.public(),
            "usage": _usage_payload(org),
        })

    if request.method == "DELETE":
        if key is None:
            return Response({"message": "No key is connected."}, status=404)
        provider, last_four = key.provider, key.last_four
        key.delete()
        _log(org, user, "disconnected", provider, last_four)
        return Response({"message": "Key disconnected.", "key": None, "usage": _usage_payload(org)})

    # PUT: connect or change. The key is tested on the sample before anything is stored.
    data = request.data or {}
    provider = str(data.get("provider") or "").strip()
    if provider not in registry.BY_ID:
        return Response({"message": "Pick a provider from the list."}, status=400)
    plain = str(data.get("key") or "").strip()
    if len(plain) < 8:
        return Response({"message": "That does not look like a key."}, status=400)
    model = str(data.get("model") or "").strip() or registry.get(provider).get("recommended_model", "")
    base_url = str(data.get("base_url") or "").strip()
    if provider != "custom":
        base_url = ""
    if _rate_limited(org):
        return Response({"message": "Five tests in ten minutes is the limit. Give it a moment."}, status=429)
    ok, message, rows, ms = run_test(provider, model, base_url, plain)
    if not ok:
        _log(org, user, "tested", provider, plain[-4:], f"failed: {message}")
        return Response({"message": message, "ok": False, "rows": rows, "ms": ms}, status=400)
    action = "changed" if key is not None else "connected"
    if key is None:
        key = OrganizationAiKey(organization=org)
    key.provider, key.model, key.base_url = provider, model, base_url
    key.set_key(plain)
    key.added_by = user
    key.last_tested_at = timezone.now()
    key.last_test_ok = True
    key.last_error = ""
    key.consecutive_failures = 0
    key.save()
    _log(org, user, action, provider, key.last_four, message)
    return Response({"message": message, "ok": True, "rows": rows, "ms": ms,
                     "key": _key_payload(key), "usage": _usage_payload(org)})


@api_view(["POST"])
def ai_key_test(request, slug):
    """Test a pasted key (body carries it) or the saved one (empty body). Never stores anything."""
    user, org, err = _org_and_gate(request, slug)
    if err:
        return err
    if _rate_limited(org):
        return Response({"message": "Five tests in ten minutes is the limit. Give it a moment."}, status=429)
    data = request.data or {}
    plain = str(data.get("key") or "").strip()
    key = OrganizationAiKey.objects.filter(organization=org).first()
    if plain:
        provider = str(data.get("provider") or "").strip()
        if provider not in registry.BY_ID:
            return Response({"message": "Pick a provider from the list."}, status=400)
        model = str(data.get("model") or "").strip() or registry.get(provider).get("recommended_model", "")
        base_url = str(data.get("base_url") or "").strip() if provider == "custom" else ""
    elif key is not None:
        provider, model, base_url, plain = key.provider, key.model, key.base_url, key.get_key()
        if not plain:
            return Response({"message": "The saved key cannot be opened any more. Paste it again.", "ok": False}, status=400)
    else:
        return Response({"message": "Paste a key first.", "ok": False}, status=400)
    ok, message, rows, ms = run_test(provider, model, base_url, plain)
    if key is not None and not data.get("key"):
        key.last_tested_at = timezone.now()
        key.last_test_ok = ok
        key.last_error = "" if ok else message
        if ok:
            key.consecutive_failures = 0
        key.save(update_fields=["last_tested_at", "last_test_ok", "last_error", "consecutive_failures", "updated_at"])
    _log(org, user, "tested", provider, plain[-4:], message if ok else f"failed: {message}")
    return Response({"message": message, "ok": ok, "rows": rows, "ms": ms, "model": model}, status=200 if ok else 400)


@api_view(["GET"])
def ai_key_usage(request, slug):
    _user, org, err = _org_and_gate(request, slug)
    if err:
        return err
    return Response(_usage_payload(org))


@api_view(["GET"])
def ai_key_history(request, slug):
    _user, org, err = _org_and_gate(request, slug)
    if err:
        return err
    rows = OrganizationAiKeyEvent.objects.filter(organization=org).select_related("actor")[:50]
    return Response({"events": [{
        "action": e.action, "provider": e.provider, "last_four": e.last_four, "detail": e.detail,
        "actor": getattr(e.actor, "username", None), "at": e.created_at.isoformat(),
    } for e in rows]})


# ── admin endpoints ─────────────────────────────────────────────────────────────────────────
def _admin_gate(request):
    user, err = _auth(request)
    if err:
        return None, err
    if not is_platform_org_admin(user):
        return None, Response({"message": "Admins only."}, status=403)
    return user, None


@api_view(["GET"])
def admin_ai_keys(request):
    """Every organization with its key (provider + model + last four, never the key), reads this
    month and all time, free reads left, OCR switch. Plus AFC's own key's reads this month."""
    from afc_ocr.models import OcrUsage
    _user, err = _admin_gate(request)
    if err:
        return err
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    keys = {k.organization_id: k for k in OrganizationAiKey.objects.all()}
    month = OcrUsage.objects.filter(created_at__gte=month_start, organization__isnull=False).values("organization_id").annotate(n=Count("id"), usd=Sum("cost_estimate_usd"))
    all_time = OcrUsage.objects.filter(organization__isnull=False).values("organization_id").annotate(n=Count("id"))
    m = {r["organization_id"]: r for r in month}
    a = {r["organization_id"]: r for r in all_time}
    orgs = Organization.objects.exclude(status="deleted").order_by("name")
    rows = []
    for org in orgs:
        k = keys.get(org.pk)
        rows.append({
            "organization_id": org.pk, "slug": org.slug, "name": org.name,
            "provider": k.provider if k else None, "model": k.model if k else None,
            "last_four": k.last_four if k else None, "last_test_ok": k.last_test_ok if k else None,
            "last_error": k.last_error if k else "",
            "reads_month": m.get(org.pk, {}).get("n", 0),
            "estimated_usd_month": float(m.get(org.pk, {}).get("usd") or 0),
            "reads_all_time": a.get(org.pk, {}).get("n", 0),
            "free_reads_left": org.ocr_free_reads_left, "ocr_disabled": org.ocr_disabled,
        })
    afc_month = OcrUsage.objects.filter(created_at__gte=month_start, paid_by__in=["afc", "afc_free"]).aggregate(
        n=Count("id"), usd=Sum("cost_estimate_usd"))
    return Response({"organizations": rows,
                     "afc_key_month": {"reads": afc_month["n"], "estimated_usd": float(afc_month["usd"] or 0)}})


@api_view(["PUT"])
def admin_set_allowance(request, org_id):
    user, err = _admin_gate(request)
    if err:
        return err
    org = Organization.objects.filter(pk=org_id).first()
    if not org:
        return Response({"message": "Organization not found."}, status=404)
    try:
        n = int(request.data.get("free_reads_left"))
    except (TypeError, ValueError):
        return Response({"message": "free_reads_left must be a whole number."}, status=400)
    if n < 0 or n > 100000:
        return Response({"message": "free_reads_left must be between 0 and 100000."}, status=400)
    org.ocr_free_reads_left = n
    org.save(update_fields=["ocr_free_reads_left"])
    _log(org, user, "allowance", detail=f"free reads set to {n}")
    return Response({"message": f"Free reads for {org.name} set to {n}.", "free_reads_left": n})


@api_view(["PUT"])
def admin_set_ocr_disabled(request, org_id):
    user, err = _admin_gate(request)
    if err:
        return err
    org = Organization.objects.filter(pk=org_id).first()
    if not org:
        return Response({"message": "Organization not found."}, status=404)
    disabled = str(request.data.get("disabled", "")).strip().lower() in ("1", "true", "yes", "on")
    org.ocr_disabled = disabled
    org.save(update_fields=["ocr_disabled"])
    _log(org, user, "disabled" if disabled else "enabled")
    return Response({"message": f"OCR {'switched off' if disabled else 'switched on'} for {org.name}.", "ocr_disabled": disabled})
