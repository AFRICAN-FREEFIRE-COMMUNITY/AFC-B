"""
AFC Bot — Website Scraper (manual run)
======================================
Thin wrapper around afc_scraper — the single source of truth shared with
bot.py:_do_scrape() and scripts/scrape_knowledge.py. Run this to refresh the
knowledge_base.txt the bot reads on every reply.

Usage:
    python scrape_site.py
"""

import os
import sys

# Keep emoji prints from crashing a non-UTF-8 console (e.g. Windows cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import afc_scraper  # noqa: E402


def run():
    print("🌐  Building AFC knowledge base (crawl pages + live teams directory)...")
    print(f"    Target: {afc_scraper.SITE_BASE}\n")
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base.txt")
    chars = afc_scraper.write_knowledge_base(dest)
    if chars:
        print(f"\n✅  knowledge_base.txt updated — {chars:,} total characters")
        print("🔄  The bot will use the new content automatically on its next reply.")
    else:
        print("\n⚠️  Nothing written — site/API returned too little. Existing file kept.")


if __name__ == "__main__":
    run()
