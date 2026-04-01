#!/usr/bin/env python3
"""
AFC Free Fire OCR Test Script
Sends match result screenshots to Gemini Vision and extracts structured player data.

Usage:
  python ocr_test.py              # process all screenshots
  python ocr_test.py 1 3 5        # process only screenshots #1, #3, #5 (1-indexed)
  python ocr_test.py --list       # list available screenshots without processing
"""

import base64
import io
import json
import sys
from pathlib import Path

import requests

# Force UTF-8 output on Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Config ─────────────────────────────────────────────────────────────────────

API_KEY = "AIzaSyBhTKBCqVJsADNSWbp4YXXqQ5N-4ER1IHU"
MODEL = "gemini-2.5-pro"  # or "gemini-2.5-flash" for faster/cheaper

SCREENSHOTS_DIR = Path(r"C:\Users\Sweez\Downloads\freefire screenshots")

# ── Prompt ─────────────────────────────────────────────────────────────────────

PROMPT = """You are analyzing a Free Fire battle royale match result screen.

This is a TEAM match. The screen shows placements with teams/players listed.
The layout may be split into two columns (e.g. placements 1-5 on the left, 6-11 on the right).
Each placement row contains 2 players from the same team.
"Eliminations" next to a player = their kill count.

Return a JSON object with this exact structure:
{
  "match_type": "team",
  "placements": [
    {
      "placement": 1,
      "players": [
        {"name": "PlayerName", "kills": 3},
        {"name": "OtherPlayer", "kills": 1}
      ]
    },
    {
      "placement": 2,
      "players": [
        {"name": "AnotherPlayer", "kills": 0},
        {"name": "LastPlayer", "kills": 2}
      ]
    }
  ]
}

Critical rules:
- Include ALL placements visible in the image (both left and right columns)
- Player names may have underscores, dots, numbers, symbols — copy EXACTLY as shown
- If a character is unclear, copy your best guess (do not skip players)
- "kills" is always an integer (0 if no kills shown)
- Return ONLY the raw JSON object — no markdown, no triple backticks, no explanation
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png" if suffix == ".png" else "image/webp"
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), mime


def call_gemini(image_path: Path) -> dict:
    b64, mime = encode_image(image_path)

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta"
        f"/models/{MODEL}:generateContent?key={API_KEY}"
    )
    resp = requests.post(url, json=payload, timeout=90)
    resp.raise_for_status()

    raw = resp.json()
    text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if model ignores responseMimeType
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(text)


def print_result(filename: str, result: dict):
    sep = "-" * 60
    print(f"\n{sep}")
    print(f"  {filename}")
    print(sep)

    placements = result.get("placements", [])
    if not placements:
        print("  (no placements extracted)")
        return

    for entry in sorted(placements, key=lambda x: x.get("placement", 99)):
        place = entry.get("placement", "?")
        players = entry.get("players", [])
        label = f"#{place}"
        if players:
            first = players[0]
            print(f"  {label:<5}  {first.get('name', '?'):<28}  {first.get('kills', 0)} kill(s)")
            for p in players[1:]:
                print(f"         {p.get('name', '?'):<28}  {p.get('kills', 0)} kill(s)")
        else:
            print(f"  {label:<5}  (no players)")


def list_screenshots() -> list[Path]:
    shots = sorted(
        list(SCREENSHOTS_DIR.glob("*.jpeg"))
        + list(SCREENSHOTS_DIR.glob("*.jpg"))
        + list(SCREENSHOTS_DIR.glob("*.png"))
        + list(SCREENSHOTS_DIR.glob("*.webp")),
        key=lambda p: p.name,
    )
    return shots


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SCREENSHOTS_DIR.exists():
        print(f"ERROR: Screenshots directory not found:\n  {SCREENSHOTS_DIR}")
        sys.exit(1)

    screenshots = list_screenshots()

    if not screenshots:
        print(f"No screenshots found in:\n  {SCREENSHOTS_DIR}")
        sys.exit(1)

    # --list flag
    if "--list" in sys.argv:
        print(f"Found {len(screenshots)} screenshot(s):\n")
        for i, p in enumerate(screenshots, 1):
            print(f"  [{i:>2}] {p.name}")
        return

    # Filter by index args (1-indexed)
    index_args = [a for a in sys.argv[1:] if a.isdigit()]
    if index_args:
        selected = []
        for idx in index_args:
            i = int(idx) - 1
            if 0 <= i < len(screenshots):
                selected.append(screenshots[i])
            else:
                print(f"WARNING: index {idx} out of range (max {len(screenshots)})")
        screenshots = selected

    print(f"\nModel : {MODEL}")
    print(f"Images: {len(screenshots)}")
    print(f"Dir   : {SCREENSHOTS_DIR}\n")

    all_results = []

    for i, path in enumerate(screenshots, 1):
        print(f"[{i}/{len(screenshots)}] {path.name}")
        try:
            result = call_gemini(path)
            all_results.append({"file": path.name, "result": result, "error": None})
            print_result(path.name, result)
        except requests.HTTPError as e:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"  ERROR: {msg}")
            all_results.append({"file": path.name, "result": None, "error": msg})
        except json.JSONDecodeError as e:
            print(f"  ERROR: Could not parse Gemini response as JSON — {e}")
            all_results.append({"file": path.name, "result": None, "error": str(e)})
        except Exception as e:
            print(f"  ERROR: {e}")
            all_results.append({"file": path.name, "result": None, "error": str(e)})

    # Save raw output
    out_path = Path(__file__).parent / "ocr_test_output.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    ok = sum(1 for r in all_results if r["error"] is None)
    print(f"\n{'-'*60}")
    print(f"Done. {ok}/{len(all_results)} succeeded.")
    print(f"Full JSON -> {out_path}")


if __name__ == "__main__":
    main()
