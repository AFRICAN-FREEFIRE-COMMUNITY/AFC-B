"""afc_auth.client_ip - the ONE answer to "what is this visitor's address" (inbox #141, owner 2026-10-04: "fix").

WHY THIS FILE EXISTS
--------------------
Six places worked the address out for themselves (the feedback form, the partner application, QR
links, referral clicks, the Turnstile check, the audit log, sign-in's device and country records), and
every one of them took the FIRST entry of X-Forwarded-For. That entry is written by the visitor: nginx
APPENDS to whatever X-Forwarded-For the request arrived with ($proxy_add_x_forwarded_for in
deploy/vps/nginx-afc.conf). A script could therefore send a new X-Forwarded-For on every request,
look like a new person each time, and walk straight past every per-address limit: five feedback posts
an hour, the partner application's limit, QR link creation, referral click counting.

WHAT IS TRUE ON THE SERVER (read on the box, 4 Oct 2026)
---------------------------------------------------------
- The API's DNS points straight at the server, not through Cloudflare, so the connection nginx sees
  IS the visitor. nginx puts it in X-Real-IP ($remote_addr) on every proxied location and passes the
  request to gunicorn on 127.0.0.1. A visitor cannot set X-Real-IP: nginx overwrites it.
- The website's own server (the Next.js container) also calls the API, from the Docker bridge
  (172.17.0.3). For one call, the QR scan (frontend app/q/[token]/route.ts), it forwards the scanner's
  address in X-Forwarded-For, because otherwise every scan would look like the website.

THE RULE
--------
1. The caller is X-Real-IP, or REMOTE_ADDR when there is no nginx in front (a developer's machine).
2. If the caller is a public address, that is the visitor. X-Forwarded-For is ignored.
3. If the caller is a private or loopback address, it can only be one of our own servers (nobody on
   the internet reaches the API from a private address), so the visitor address it forwards in the
   first X-Forwarded-For entry is believed, provided it parses as an address.

CALLERS: afc_auth.views.get_client_ip (sign-in, devices, recovery, signup referral claim),
afc_auth.middleware (audit log), afc_auth.bot_protection (Turnstile remoteip), afc_feedback,
afc_partner_apply, afc_qr, afc_referrals, and afc_helpbot (the website Help panel, branch
feature/help-bot, which used to read X-Real-IP itself). tools/known_bugs.json (be-forwarded-for-read)
fails any new read of X-Forwarded-For or X-Real-IP outside this file, so a ninth copy cannot come back.
"""
import ipaddress


def _parse(value):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_our_server(address) -> bool:
    ip = _parse(address)
    return ip is not None and (ip.is_loopback or ip.is_private)


def client_ip(request) -> str:
    """The visitor's address as a string, or "" when there is none. Never raises."""
    meta = getattr(request, "META", {}) or {}
    caller = (meta.get("HTTP_X_REAL_IP") or meta.get("REMOTE_ADDR") or "").strip()
    if _is_our_server(caller):
        forwarded = (meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        if _parse(forwarded) is not None:
            return forwarded
    return caller
