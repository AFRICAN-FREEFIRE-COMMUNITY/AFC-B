"""afc_polls.vote_tracking - what the server notes about a vote, and the safety flags read from it.

WHY THIS EXISTS (inbox #453 and #457, owner 2 Nov 2026)
-------------------------------------------------------
"I need you to be able to track these things, locations of who voted then, countries the votes came
from for each nominee." The owner then picked every item on deck Revision 9 slides 05 and 06: votes
per hour and day, pick switches, which nominee's link brought the voter, device, account age,
started vs finished ballots, and safety flags that an admin reviews before the count.

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
On PollResponse at submit (capture() below): IP country code and region (afc_polls.geo, a local
file), a keyed hash of the IP and of the browser's device key, the device type from the User-Agent,
the account's age, the referring nominee, and voter_key. NEVER the raw IP, never the device key
itself, never a user id on an anonymous poll. The hashes use Poll.tracking_key, so they cannot be
matched across polls, and they can only say "these ballots share a connection / a device".

THE FLAGS (compute_flags)
-------------------------
Computed when read, never stored, so a threshold change applies to every ballot at once:
  country_mismatch   the profile country (eligibility_snapshot) and the IP country are both known
                     and differ. Common and innocent with VPNs and travel, hence review, not removal.
  shared_connection  SHARED_CONNECTION_MIN or more ballots on this poll came over one connection.
                     Set high on purpose: a cyber cafe or a family on one router is normal here.
  shared_device      SHARED_DEVICE_MIN or more ballots came from one browser.
  new_account        the account was under NEW_ACCOUNT_DAYS days old when it voted.
  failed_checks      the voter was refused by the eligibility check, or failed to link a UID, at
                     least once on this poll (PollSheetOpen.failed_checks).
An admin reviews each flagged ballot (afc_polls.views.admin_review_response): Looks fine, or Remove
with a written reason. Removed ballots leave every tally (COUNTED below).

CALLERS: afc_polls.views (submit_response, sheet_opened, admin_edition_metrics, admin_poll_flags,
live_view) and afc_polls.team_voting (the removed-ballot exclusion).
"""
import hashlib
import hmac
import re
from collections import Counter

from django.db.models import Q

from afc_auth.client_ip import client_ip
from afc_auth.country_grouping import country_code

from .geo import region_for_ip
from .models import PollOption, PollResponse, PollSheetOpen

# ── thresholds (read by compute_flags; the Metrics tab prints them beside the flag names) ─────────
SHARED_CONNECTION_MIN = 3
SHARED_DEVICE_MIN = 2
NEW_ACCOUNT_DAYS = 3

# Lower bounds of the account-age buckets. Used to bucket the stored value on an anonymous poll and
# to group every poll's ages on the Metrics tab. Includes NEW_ACCOUNT_DAYS so the flag still works
# on a bucketed value.
ACCOUNT_AGE_BUCKETS = (0, 1, 3, 7, 30, 365)

FLAG_KEYS = ("country_mismatch", "shared_connection", "shared_device", "new_account", "failed_checks")

# A ballot counts unless an admin removed it. Every tally in afc_polls goes through this, so a
# removal can never be applied in one place and forgotten in another.
COUNTED = ~Q(review_state=PollResponse.REVIEW_REMOVED)
COUNTED_ANSWER = ~Q(response__review_state=PollResponse.REVIEW_REMOVED)

_DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TABLET_RE = re.compile(r"ipad|tablet|kindle|silk|playbook|(android(?!.*mobile))", re.I)
_PHONE_RE = re.compile(r"mobi|iphone|ipod|windows phone|opera mini|blackberry|android.*mobile", re.I)


def keyed_hash(poll, value):
    """HMAC-SHA256(Poll.tracking_key, value) as hex, or "" for no value."""
    if not value:
        return ""
    key = poll.ensure_tracking_key().encode()
    return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()


def voter_key(poll, user):
    """The pseudonym that joins a ballot to its PollSheetOpen row. Same on every poll type."""
    return keyed_hash(poll, f"voter:{user.pk}")


def device_type(user_agent):
    """"phone", "tablet", "computer", or "" when the browser sent no User-Agent."""
    ua = (user_agent or "").strip()
    if not ua:
        return ""
    if _TABLET_RE.search(ua):
        return "tablet"
    if _PHONE_RE.search(ua):
        return "phone"
    return "computer"


def age_bucket(days):
    """The lower bound of the bucket `days` falls in (0, 1, 3, 7, 30 or 365), or None."""
    if days is None:
        return None
    found = ACCOUNT_AGE_BUCKETS[0]
    for bound in ACCOUNT_AGE_BUCKETS:
        if days >= bound:
            found = bound
    return found


def clean_device_key(raw):
    """The browser's device key if it looks like one (frontend lib/voteDevice.ts), else ""."""
    text = str(raw or "").strip()
    return text if _DEVICE_KEY_RE.match(text) else ""


def referring_option(poll, raw):
    """The PollOption the visit started on, only if it belongs to THIS poll; else None."""
    try:
        option_id = int(raw)
    except (TypeError, ValueError):
        return None
    return PollOption.objects.filter(option_id=option_id, question__poll=poll).first()


def capture(poll, request, user, response, now):
    """Stamp the tracking fields on `response` (not saved here; submit_response saves it).

    Re-run on every re-save, so the location and device are those of the LATEST vote. The referring
    nominee is first-touch: once a ballot has one, a later visit that started elsewhere keeps it.
    """
    geo = region_for_ip(client_ip(request))
    response.ip_country = geo["country"]
    response.ip_region = geo["region"]
    response.connection_hash = keyed_hash(poll, client_ip(request))
    device_key = clean_device_key(request.data.get("device_key"))
    if device_key:
        response.device_hash = keyed_hash(poll, f"device:{device_key}")
    response.device_type = device_type(request.META.get("HTTP_USER_AGENT"))
    joined = getattr(user, "date_joined", None)
    days = max(0, (now - joined).days) if joined else None
    response.account_age_days = age_bucket(days) if poll.anonymous else days
    if response.referrer_option_id is None:
        option = referring_option(poll, request.data.get("ref_option_id"))
        if option is not None:
            response.referrer_option = option
    response.voter_key = voter_key(poll, user)


def record_sheet_open(poll, user, failed_check=False):
    """Create this voter's started-ballot row if missing; add a failed check when asked."""
    row, _ = PollSheetOpen.objects.get_or_create(poll=poll, voter_key=voter_key(poll, user))
    if failed_check:
        PollSheetOpen.objects.filter(pk=row.pk).update(failed_checks=row.failed_checks + 1)
        row.failed_checks += 1
    return row


def compute_flags(poll):
    """{response_id: {"flags": [keys], "details": {...}}} for every submitted ballot that has a flag.

    One pass over the poll's submitted ballots plus one over its sheet opens; the hash groups are
    counted in Python because they are compared against each other, not against a constant.
    """
    rows = list(
        PollResponse.objects.filter(poll=poll, status=PollResponse.SUBMITTED).values(
            "response_id", "eligibility_snapshot", "ip_country", "ip_region", "connection_hash",
            "device_hash", "account_age_days", "voter_key",
        )
    )
    connections = Counter(row["connection_hash"] for row in rows if row["connection_hash"])
    devices = Counter(row["device_hash"] for row in rows if row["device_hash"])
    failed = dict(
        PollSheetOpen.objects.filter(poll=poll, failed_checks__gt=0).values_list("voter_key", "failed_checks")
    )

    flagged = {}
    for row in rows:
        snapshot = row["eligibility_snapshot"] if isinstance(row["eligibility_snapshot"], dict) else {}
        profile_code = country_code(snapshot.get("country")) or ""
        details = {
            "profile_country": profile_code,
            "ip_country": row["ip_country"],
            "ip_region": row["ip_region"],
            "connection_ballots": connections.get(row["connection_hash"], 0) if row["connection_hash"] else 0,
            "device_ballots": devices.get(row["device_hash"], 0) if row["device_hash"] else 0,
            "account_age_days": row["account_age_days"],
            "failed_checks": failed.get(row["voter_key"], 0) if row["voter_key"] else 0,
        }
        flags = []
        if profile_code and row["ip_country"] and profile_code != row["ip_country"]:
            flags.append("country_mismatch")
        if details["connection_ballots"] >= SHARED_CONNECTION_MIN:
            flags.append("shared_connection")
        if details["device_ballots"] >= SHARED_DEVICE_MIN:
            flags.append("shared_device")
        if row["account_age_days"] is not None and row["account_age_days"] < NEW_ACCOUNT_DAYS:
            flags.append("new_account")
        if details["failed_checks"] > 0:
            flags.append("failed_checks")
        if flags:
            flagged[row["response_id"]] = {"flags": flags, "details": details}
    return flagged
