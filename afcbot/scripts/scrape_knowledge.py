"""
AFC Knowledge Base Scraper (GitHub Actions entry point)
Runs via .github/workflows/update_knowledge.yml every 3 hours, commits any change
to knowledge_base.txt in the repo.

Thin wrapper around afc_scraper — the single source of truth shared with
bot.py:_do_scrape() and scrape_site.py — so the three scrapers can never drift.
The Action installs `requests` + `beautifulsoup4`, which is all afc_scraper needs.
"""

import os
import sys

# Keep emoji prints from crashing a non-UTF-8 console (e.g. Windows cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The Action invokes this as `python scripts/scrape_knowledge.py`, so sys.path[0]
# is the scripts/ dir. Put the repo root on the path so `import afc_scraper` works.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import afc_scraper  # noqa: E402


def run():
    print(f"🌐 AFC Knowledge Base Scraper — target {afc_scraper.SITE_BASE}\n")
    chars = afc_scraper.write_knowledge_base(os.path.join(REPO_ROOT, "knowledge_base.txt"))
    if chars:
        print(f"\n✅ knowledge_base.txt updated — {chars:,} chars")
    else:
        print("\n⚠️  Too little content scraped — file not updated")


if __name__ == "__main__":
    run()
