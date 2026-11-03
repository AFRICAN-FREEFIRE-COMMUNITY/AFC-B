"""afc_polls.geo - where an IP address is, from a LOCAL file, with no account and no network call.

WHY A LOCAL FILE (inbox #453, owner 2 Nov 2026: "locations of who voted then, countries the votes came
from for each nominee")
---------------------------------------------------------------------------------------------------
The sign-in path already asks ipinfo.io (afc_auth.views.geo_for_ip), but that is a billed API with a
monthly cap and it hands every voter's address to a third party. An awards vote can be 50,000 ballots
in a fortnight, so each vote asking ipinfo would blow the cap and leak the list. Instead the server
keeps DB-IP's free "IP to City Lite" database (CC BY 4.0, no account, refreshed monthly by
`manage.py refresh_geoip_db`) and reads it in-process with MaxMind's `maxminddb` reader. MaxMind's own
GeoLite2 would need an account and a licence key, which is the owner's step; DB-IP needs neither.

The licence asks for attribution where the data is shown: the admin Metrics tab and Garena's live page
both print "Location data: DB-IP" (frontend AwardsMetrics.tsx, app/(user)/awards/live/[token]).

WHAT IT RETURNS
---------------
region_for_ip("41.58.0.1") -> {"country": "NG", "region": "Lagos"}
Only the ISO country code and the first-level region (state or province). Never the city or the
coordinates: the owner asked for region, and a city plus a time is close to naming a voter. Private,
loopback and malformed addresses, a missing file and any reader error all give {"country": "",
"region": ""}: geo is a nice-to-have on a vote and must never refuse or slow one.

CALLERS: afc_polls.vote_tracking.capture (at submit). The file path is settings.GEOIP_CITY_DB.
"""
import ipaddress
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

EMPTY = {"country": "", "region": ""}

_reader = None
_reader_path = None
_reader_lock = threading.Lock()


def _open_reader():
    """The process-wide reader, opened once and reopened when the file on disk is replaced."""
    global _reader, _reader_path
    path = str(getattr(settings, "GEOIP_CITY_DB", "") or "")
    if not path:
        return None
    with _reader_lock:
        if _reader is not None and _reader_path == path:
            return _reader
        try:
            import maxminddb

            _reader = maxminddb.open_database(path)
            _reader_path = path
        except Exception:  # missing file, unreadable file, package not installed: geo is optional
            logger.warning("GeoIP database not available at %s", path)
            _reader, _reader_path = None, None
        return _reader


def reset_reader():
    """Drop the open reader so the next lookup reopens the file (refresh_geoip_db calls this)."""
    global _reader, _reader_path
    with _reader_lock:
        if _reader is not None:
            try:
                _reader.close()
            except Exception:  # closing a reader that already failed is harmless either way
                logger.debug("GeoIP reader close failed", exc_info=True)
        _reader, _reader_path = None, None


def region_for_ip(ip):
    """{"country": ISO alpha-2 or "", "region": first-level region or ""}. Never raises."""
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return dict(EMPTY)
    if not address.is_global:
        return dict(EMPTY)
    reader = _open_reader()
    if reader is None:
        return dict(EMPTY)
    try:
        record = reader.get(str(address)) or {}
    except Exception:  # a corrupt record must not break a vote
        logger.warning("GeoIP lookup failed", exc_info=True)
        return dict(EMPTY)
    country = ((record.get("country") or {}).get("iso_code") or "").upper()[:2]
    subdivisions = record.get("subdivisions") or []
    region = ""
    if subdivisions:
        region = ((subdivisions[0].get("names") or {}).get("en") or "")[:100]
    return {"country": country, "region": region}
