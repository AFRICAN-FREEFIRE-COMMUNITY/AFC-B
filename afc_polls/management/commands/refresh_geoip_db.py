"""manage.py refresh_geoip_db - download DB-IP's free city database for the vote tracking.

WHY (inbox #453, owner 2 Nov 2026): the awards Metrics tab shows the country and region each vote came
from. afc_polls.geo reads them from a local file so no voter address ever leaves the server and no
per-vote API is billed. DB-IP publishes "IP to City Lite" every month under CC BY 4.0, with no account
and no key: https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz

WHAT IT DOES
  1. Tries this month's file, then last month's (a new month's file appears a day or two in).
  2. Streams the download to a temporary file beside the target, unpacks it, opens it with the same
     reader the site uses and looks up a known public address, so a truncated or wrong file is
     refused before it can replace a good one.
  3. Swaps it into place in one rename (settings.GEOIP_CITY_DB) and tells afc_polls.geo to reopen.

  --if-missing     do nothing when the file is already there (the deploy uses this, so a deploy
                   never waits on a 60 MB download it does not need)
  --max-age DAYS   do nothing when the file is younger than DAYS (the monthly beat task uses 25)

CALLED BY: deploy/vps/deploy-backend.sh (--if-missing, never fails the deploy) and
afc_polls.tasks.refresh_geoip_db (celery beat, monthly; afc/celery_config.py).
"""
import gzip
import os
import shutil
import tempfile
import time
from datetime import date

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from afc_polls import geo

URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{year:04d}-{month:02d}.mmdb.gz"
DOWNLOAD_TIMEOUT_SECONDS = 120
# A well-known public resolver address: any real city database places it in the United States.
PROBE_ADDRESS = "8.8.8.8"
PROBE_COUNTRY = "US"


def _months_to_try(today):
    """This month, then the month before."""
    previous = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return [(today.year, today.month), previous]


class Command(BaseCommand):
    help = "Download DB-IP's free city database used by the awards vote tracking (afc_polls.geo)."

    def add_arguments(self, parser):
        parser.add_argument("--if-missing", action="store_true", help="Skip when the file already exists.")
        parser.add_argument("--max-age", type=int, default=None, help="Skip when the file is younger than this many days.")

    def handle(self, *args, **options):
        target = str(settings.GEOIP_CITY_DB)
        if os.path.exists(target):
            if options["if_missing"]:
                self.stdout.write(f"GeoIP database already present at {target}")
                return
            if options["max_age"] is not None:
                age_days = (time.time() - os.path.getmtime(target)) / 86400
                if age_days < options["max_age"]:
                    self.stdout.write(f"GeoIP database is {age_days:.0f} days old, younger than {options['max_age']}")
                    return
        os.makedirs(os.path.dirname(target), exist_ok=True)

        last_error = ""
        for year, month in _months_to_try(date.today()):
            url = URL_TEMPLATE.format(year=year, month=month)
            try:
                self._install(url, target)
            except CommandError as exc:
                last_error = str(exc)
                self.stdout.write(f"{url}: {exc}")
                continue
            geo.reset_reader()
            self.stdout.write(self.style.SUCCESS(f"GeoIP database installed from {url} at {target}"))
            return
        raise CommandError(f"No DB-IP city database could be installed: {last_error}")

    def _install(self, url, target):
        folder = os.path.dirname(target)
        gz_fd, gz_path = tempfile.mkstemp(suffix=".mmdb.gz", dir=folder)
        db_fd, db_path = tempfile.mkstemp(suffix=".mmdb", dir=folder)
        os.close(gz_fd)
        os.close(db_fd)
        try:
            try:
                with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    if response.status_code != 200:
                        raise CommandError(f"download answered {response.status_code}")
                    with open(gz_path, "wb") as out:
                        for chunk in response.iter_content(chunk_size=1 << 20):
                            out.write(chunk)
                with gzip.open(gz_path, "rb") as packed, open(db_path, "wb") as out:
                    shutil.copyfileobj(packed, out)
            except (requests.RequestException, OSError, EOFError) as exc:
                raise CommandError(f"download or unpack failed: {exc}") from exc
            self._verify(db_path)
            os.replace(db_path, target)
        finally:
            for leftover in (gz_path, db_path):
                if os.path.exists(leftover):
                    os.remove(leftover)

    def _verify(self, path):
        try:
            import maxminddb

            with maxminddb.open_database(path) as reader:
                record = reader.get(PROBE_ADDRESS) or {}
        except Exception as exc:  # any reader failure means the file is not usable
            raise CommandError(f"the downloaded file is not a readable database: {exc}") from exc
        found = (record.get("country") or {}).get("iso_code")
        if found != PROBE_COUNTRY:
            raise CommandError(f"the downloaded file places {PROBE_ADDRESS} in {found!r}, not {PROBE_COUNTRY}")
