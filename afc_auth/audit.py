"""
Rich, view-authored audit summaries.

The AuditLogMiddleware (afc_auth/middleware.py) auto-logs every admin/staff mutation, but it can only
derive a GENERIC summary from the URL ("Edited an event #163") - it does not know the entity's name
or the before/after of a field. A VIEW does. So an instrumented view calls set_audit() to supply the
specific, human summary (and any extra detail fields), and the middleware uses it instead of the
generic one when building the AuditLog row for that request.

Example (in a view):
    set_audit(request, f"Changed {event.event_name} from {old_type} to {new_type}")
    set_audit(request, f"Sent broadcast \"{title}\" to {count} players in {group.name}",
              recipients=count)

This module deliberately has NO model imports so it is import-light and safe to import from any view.
The middleware reads request.audit_summary / request.audit_details; un-instrumented endpoints simply
fall back to the generic summary.
"""


def set_audit(request, summary, **details):
    """Attach a specific, human audit summary (+ optional detail fields) to the current request.
    The AuditLogMiddleware prefers this over its generic slug-derived summary. Safe to call
    unconditionally - if auditing is skipped for this request (non-admin, read, etc.) it is simply
    ignored. Keep `summary` a single readable sentence, e.g.
    "Changed Detty December from internal to external"."""
    try:
        # In a DRF @api_view the `request` is a rest_framework.request.Request WRAPPER; the
        # AuditLogMiddleware only sees the underlying Django HttpRequest. Attributes set on the
        # wrapper are invisible to the middleware, so write to request._request when present.
        target = getattr(request, "_request", None) or request
        target.audit_summary = (summary or "")[:255]
        if details:
            merged = getattr(target, "audit_details", {}) or {}
            merged.update(details)
            target.audit_details = merged
    except Exception:
        # Auditing must never break a request - swallow anything odd (e.g. no request object).
        pass
