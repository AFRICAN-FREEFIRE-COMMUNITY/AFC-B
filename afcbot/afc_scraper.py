"""
afc_scraper.py — single source of truth for building knowledge_base.txt.

Used by all three entry points so they can never drift again:
  - bot.py:_do_scrape()          live on the box, every SCRAPE_INTERVAL_HOURS
  - scripts/scrape_knowledge.py  GitHub Action, every 3h, commits the repo copy
  - scrape_site.py               manual `python scrape_site.py`

Behavior:
  - Crawls from SEED_PAGES, following internal links (BFS) so new SSR pages are
    discovered automatically.
  - Extracts clean text with BeautifulSoup (drops script/style/nav/footer/header).
  - Skips client-rendered shells. The AFC site is Next.js App Router: data pages
    (/tournaments, /teams, /news, /home) render their content client-side, so the
    server HTML is just a nav/footer skeleton containing "Loading" / "No teams
    available". Capturing that pollutes the KB, so captures below MIN_CONTENT_CHARS
    or that are obvious placeholders are dropped. (/contact is short but real — it
    carries the support email + Discord — so the threshold keeps it.)

NOTE: the AFC teams roster is NOT scraped into the KB. Embedding 560 teams in every
reply's prompt was too heavy, so teams are served on demand via the bot's live tools
(search_teams / get_team_members in bot.py), not from this file.
"""

import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SITE_BASE = "https://africanfreefirecommunity.com"

# Starting pages — the crawler also discovers linked pages automatically.
SEED_PAGES = [
    "/home",
    "/about",
    "/rules",
    "/contact",
    "/terms-of-service",
    "/privacy-policy",
    "/tournaments",
    "/teams",
    "/news",
    "/awards",
]

# Never crawl these.
SKIP_PREFIXES = (
    "/_next", "/static", "/api", "/favicon", "/images",
    "/fonts", "/.well-known", "/login", "/create-account",
)

# Captures shorter than this (after stripping chrome) are treated as client-render
# shells, not real content, and dropped. Tuned to keep /contact (~194 chars, real:
# support email + Discord) while dropping the true shells (/home 7, /tournaments 10,
# /news 68, /teams 121 "No teams available.").
MIN_CONTENT_CHARS = 150
SHELL_MARKERS = ("Loading...", "No teams available")

HEADERS = {"User-Agent": "AFC-Bot-Scraper/1.0"}
HTTP_TIMEOUT = 15
MAX_PAGES = 60            # crawl safety cap


def _get(url, **kw):
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", HTTP_TIMEOUT)
    return requests.get(url, **kw)


def _clean_text(html: str) -> str:
    """Strip chrome + non-content tags, return collapsed visible text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav",
                     "footer", "header", "button", "img"]):
        tag.decompose()
    main = soup.find("main") or soup.find("body") or soup
    text = main.get_text(separator=" ")
    return re.sub(r"\s{2,}", " ", text).strip()


def _discover_links(html: str) -> set:
    """Internal links from the raw HTML (before chrome is stripped)."""
    soup = BeautifulSoup(html, "html.parser")
    out = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if (h.startswith("/") and len(h) > 1
                and "#" not in h and "?" not in h
                and not h.startswith(SKIP_PREFIXES)):
            out.add(h.rstrip("/") or "/")
    return out


def _is_shell(text: str) -> bool:
    """True if the captured text is a client-render placeholder / nav chrome only."""
    if len(text) < MIN_CONTENT_CHARS:
        return True
    if any(m in text for m in SHELL_MARKERS) and len(text) < 800:
        return True
    return False


def scrape_page(path: str):
    """Return (clean_text, discovered_links). Empty text on error/non-200."""
    try:
        resp = _get(SITE_BASE + path)
        if resp.status_code != 200:
            return "", set()
        return _clean_text(resp.text), _discover_links(resp.text)
    except Exception as e:
        print(f"  ⚠️  Failed: {path} — {e}")
        return "", set()


def build_knowledge_text() -> str:
    """Crawl the site and return the full KB text. (Teams are NOT included — they
    are served on demand by the bot's tools; see the module docstring.)"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [
        "============================",
        "AFC WEBSITE KNOWLEDGE",
        f"Source: {SITE_BASE}",
        f"Last Updated: {now}",
        "============================",
        "",
    ]

    queue = list(SEED_PAGES)
    scraped = set()
    captured = 0

    while queue and len(scraped) < MAX_PAGES:
        path = queue.pop(0)
        norm = path.rstrip("/") or "/"
        if norm in scraped:
            continue
        scraped.add(norm)

        text, links = scrape_page(path)

        # Enqueue discovered links regardless of whether this page is a shell —
        # a new SSR page might only be linked from a shell's nav.
        for link in links:
            if (link.rstrip("/") or "/") not in scraped and link not in queue:
                queue.append(link)

        if text and not _is_shell(text):
            sections.append(f"\n--- PAGE: {path} ---")
            sections.append(text)
            captured += 1
            print(f"  ✅  {path} — {len(text):,} chars, {len(links)} links")
        elif text:
            print(f"  ⏭️  {path} — client-render shell ({len(text)} chars), skipped")
        else:
            print(f"  ⏭️  {path} — empty/error")

    print(f"  📄  pages captured: {captured} of {len(scraped)} crawled")
    return "\n".join(sections)


def write_knowledge_base(dest: str = "knowledge_base.txt") -> int:
    """Build and write the KB. Refuses to overwrite with near-empty output
    (protects the live KB when the site/API is down). Returns chars written, or 0."""
    text = build_knowledge_text()
    if len(text) > 1000:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        return len(text)
    print("  ⚠️  too little content scraped — knowledge_base.txt left unchanged")
    return 0


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    chars = write_knowledge_base(os.path.join(base, "knowledge_base.txt"))
    print(f"\n✅  knowledge_base.txt — {chars:,} chars")
