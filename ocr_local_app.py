#!/usr/bin/env python3
"""
AFC Free Fire OCR - Local Test App
Run:  python ocr_local_app.py
Then open:  http://localhost:5050
"""

import base64
import json
import os
import io
import sys

# Force UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from flask import Flask, request, jsonify

import requests as http_requests

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY = "AIzaSyBhTKBCqVJsADNSWbp4YXXqQ5N-4ER1IHU"
MODEL   = "gemini-2.5-pro"

PROMPT = """You are analyzing a Free Fire battle royale match result screen.

This is a TEAM match. The screen shows placements with teams/players listed.
The layout may be split into two columns (e.g. placements 1-5 on the left, 6-11 on the right).
Each placement row contains players from the same team (usually 2 or 4 players).
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

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AFC OCR Tester</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d0d0f;
    color: #e4e4e7;
    min-height: 100vh;
    padding: 32px 16px;
  }

  .container { max-width: 960px; margin: 0 auto; }

  h1 {
    font-size: 1.75rem;
    font-weight: 700;
    color: #22c55e;
    margin-bottom: 4px;
  }
  .subtitle { color: #71717a; font-size: 0.875rem; margin-bottom: 32px; }

  /* Drop zone */
  #drop-zone {
    border: 2px dashed #3f3f46;
    border-radius: 12px;
    padding: 48px 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
    margin-bottom: 16px;
  }
  #drop-zone.dragover { border-color: #22c55e; background: rgba(34,197,94,0.05); }
  #drop-zone .icon { font-size: 2.5rem; margin-bottom: 12px; }
  #drop-zone p { color: #a1a1aa; font-size: 0.9rem; margin-top: 6px; }
  #drop-zone strong { color: #e4e4e7; }
  #file-input { display: none; }

  /* Thumbnails */
  #thumb-row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 20px;
    min-height: 0;
  }
  .thumb-wrap {
    position: relative;
    width: 80px;
    height: 80px;
    border-radius: 8px;
    overflow: hidden;
    border: 2px solid #3f3f46;
    flex-shrink: 0;
  }
  .thumb-wrap img { width: 100%; height: 100%; object-fit: cover; }
  .thumb-wrap .remove-btn {
    position: absolute;
    top: 2px; right: 2px;
    background: rgba(0,0,0,0.75);
    border: none;
    color: #f87171;
    font-size: 0.75rem;
    border-radius: 4px;
    cursor: pointer;
    padding: 1px 4px;
    line-height: 1.4;
  }
  .thumb-wrap .status-dot {
    position: absolute;
    bottom: 3px; left: 3px;
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #3f3f46;
  }
  .thumb-wrap .status-dot.processing { background: #f59e0b; animation: pulse 1s infinite; }
  .thumb-wrap .status-dot.done       { background: #22c55e; }
  .thumb-wrap .status-dot.error      { background: #ef4444; }

  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  /* Buttons */
  .btn-row { display: flex; gap: 10px; margin-bottom: 32px; }
  button {
    padding: 10px 22px;
    border-radius: 8px;
    border: none;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  #process-btn { background: #22c55e; color: #000; }
  #clear-btn   { background: #27272a; color: #e4e4e7; }

  /* Status bar */
  #status-bar {
    font-size: 0.85rem;
    color: #a1a1aa;
    margin-bottom: 24px;
    min-height: 1.2em;
  }

  /* Results */
  #results { display: flex; flex-direction: column; gap: 24px; }

  .result-card {
    background: #18181b;
    border: 1px solid #27272a;
    border-radius: 12px;
    overflow: hidden;
  }
  .result-card.error-card { border-color: #7f1d1d; }

  .card-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 18px;
    background: #1c1c1f;
    border-bottom: 1px solid #27272a;
  }
  .card-header img {
    width: 52px; height: 52px;
    object-fit: cover;
    border-radius: 6px;
    flex-shrink: 0;
  }
  .card-header .filename {
    font-size: 0.8rem;
    color: #71717a;
    word-break: break-all;
  }
  .card-header .badge {
    margin-left: auto;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid;
    white-space: nowrap;
  }
  .badge.ok    { color: #22c55e; border-color: #22c55e; }
  .badge.err   { color: #ef4444; border-color: #ef4444; }
  .badge.wait  { color: #f59e0b; border-color: #f59e0b; }

  .card-body { padding: 16px 18px; }

  /* Placements grid */
  .placements { display: flex; flex-direction: column; gap: 10px; }

  .placement-row {
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .place-num {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    background: #27272a;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 0.85rem;
    color: #a1a1aa;
    flex-shrink: 0;
  }
  .place-num.gold   { background: rgba(234,179,8,0.15);  color: #eab308; border: 1px solid rgba(234,179,8,0.3); }
  .place-num.silver { background: rgba(156,163,175,0.15); color: #9ca3af; border: 1px solid rgba(156,163,175,0.3); }
  .place-num.bronze { background: rgba(180,83,9,0.15);   color: #b45309; border: 1px solid rgba(180,83,9,0.3); }

  .players-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    flex: 1;
  }
  .player-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #27272a;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 0.8rem;
    white-space: nowrap;
  }
  .player-chip .kills {
    background: #3f3f46;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 0.7rem;
    color: #a1a1aa;
  }
  .player-chip .kills.high { background: rgba(34,197,94,0.2); color: #22c55e; }

  .error-msg { color: #f87171; font-size: 0.85rem; font-family: monospace; }

  /* JSON toggle */
  .json-toggle {
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid #27272a;
  }
  .json-toggle summary {
    cursor: pointer;
    font-size: 0.8rem;
    color: #52525b;
    user-select: none;
  }
  .json-toggle pre {
    margin-top: 8px;
    background: #09090b;
    border-radius: 6px;
    padding: 12px;
    font-size: 0.72rem;
    overflow-x: auto;
    color: #71717a;
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
</head>
<body>
<div class="container">
  <h1>AFC OCR Tester</h1>
  <p class="subtitle">Upload Free Fire match screenshots &rarr; Gemini extracts player data</p>

  <div id="drop-zone">
    <div class="icon">📸</div>
    <strong>Drop screenshots here</strong>
    <p>or click to browse &mdash; PNG, JPG, WEBP</p>
    <input type="file" id="file-input" accept="image/*" multiple />
  </div>

  <div id="thumb-row"></div>

  <div class="btn-row">
    <button id="process-btn" disabled>Run OCR</button>
    <button id="clear-btn">Clear All</button>
  </div>

  <div id="status-bar"></div>
  <div id="results"></div>
</div>

<script>
  const dropZone    = document.getElementById('drop-zone');
  const fileInput   = document.getElementById('file-input');
  const thumbRow    = document.getElementById('thumb-row');
  const processBtn  = document.getElementById('process-btn');
  const clearBtn    = document.getElementById('clear-btn');
  const statusBar   = document.getElementById('status-bar');
  const resultsDiv  = document.getElementById('results');

  // file list: [{file, objectURL, thumbEl, cardEl}]
  let queue = [];

  // ── Drag & drop ─────────────────────────────────────────────────────────────
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    addFiles([...e.dataTransfer.files]);
  });
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value = ''; });

  function addFiles(files) {
    files.filter(f => f.type.startsWith('image/')).forEach(file => {
      const url = URL.createObjectURL(file);

      // thumbnail
      const wrap = document.createElement('div');
      wrap.className = 'thumb-wrap';
      wrap.innerHTML = `<img src="${url}" /><div class="status-dot"></div><button class="remove-btn">✕</button>`;
      const idx = queue.length;
      wrap.querySelector('.remove-btn').addEventListener('click', e => {
        e.stopPropagation();
        removeFile(idx);
      });
      thumbRow.appendChild(wrap);

      // placeholder card
      const card = document.createElement('div');
      card.className = 'result-card';
      card.innerHTML = `
        <div class="card-header">
          <img src="${url}" />
          <span class="filename">${file.name}</span>
          <span class="badge wait">Pending</span>
        </div>`;
      resultsDiv.appendChild(card);

      queue.push({ file, url, thumbEl: wrap, cardEl: card });
    });
    updateUI();
  }

  function removeFile(idx) {
    if (queue[idx]) {
      URL.revokeObjectURL(queue[idx].url);
      queue[idx].thumbEl.remove();
      queue[idx].cardEl.remove();
      queue[idx] = null;
    }
    // Compact if all null
    if (queue.every(x => x === null)) queue = [];
    updateUI();
  }

  clearBtn.addEventListener('click', () => {
    queue.forEach(item => { if(item) URL.revokeObjectURL(item.url); });
    queue = [];
    thumbRow.innerHTML = '';
    resultsDiv.innerHTML = '';
    statusBar.textContent = '';
    updateUI();
  });

  function updateUI() {
    const active = queue.filter(Boolean);
    processBtn.disabled = active.length === 0;
  }

  // ── Process ──────────────────────────────────────────────────────────────────
  processBtn.addEventListener('click', async () => {
    const items = queue.filter(Boolean);
    if (!items.length) return;

    processBtn.disabled = true;
    clearBtn.disabled   = true;

    let done = 0;
    statusBar.textContent = `Processing 0 / ${items.length}…`;

    for (const item of items) {
      const dot = item.thumbEl.querySelector('.status-dot');
      dot.className = 'status-dot processing';

      // Update card to loading state
      const header = item.cardEl.querySelector('.card-header');
      header.querySelector('.badge').textContent = 'Processing…';
      header.querySelector('.badge').className = 'badge wait';

      try {
        const formData = new FormData();
        formData.append('image', item.file);

        const resp = await fetch('/ocr', { method: 'POST', body: formData });
        const data = await resp.json();

        if (data.error) throw new Error(data.error);

        dot.className = 'status-dot done';
        renderCard(item, data.result);
      } catch(err) {
        dot.className = 'status-dot error';
        renderError(item, err.message);
      }

      done++;
      statusBar.textContent = `Processing ${done} / ${items.length}…`;
    }

    statusBar.textContent = `Done — ${items.length} image(s) processed.`;
    processBtn.disabled = false;
    clearBtn.disabled   = false;
  });

  // ── Render helpers ───────────────────────────────────────────────────────────
  function placeClass(n) {
    if (n === 1) return 'gold';
    if (n === 2) return 'silver';
    if (n === 3) return 'bronze';
    return '';
  }

  function renderCard(item, result) {
    const placements = (result.placements || []).sort((a,b) => a.placement - b.placement);
    const totalPlayers = placements.reduce((s,p) => s + (p.players||[]).length, 0);

    let rows = placements.map(p => {
      const cls = placeClass(p.placement);
      const chips = (p.players || []).map(pl => {
        const k = pl.kills || 0;
        const highKill = k >= 5 ? 'high' : '';
        return `<span class="player-chip"><span>${escHtml(pl.name)}</span><span class="kills ${highKill}">${k}k</span></span>`;
      }).join('');
      return `<div class="placement-row">
        <div class="place-num ${cls}">#${p.placement}</div>
        <div class="players-list">${chips || '<span style="color:#52525b;font-size:0.8rem">no players</span>'}</div>
      </div>`;
    }).join('');

    item.cardEl.innerHTML = `
      <div class="card-header">
        <img src="${item.url}" />
        <div>
          <div class="filename">${escHtml(item.file.name)}</div>
          <div style="font-size:0.75rem;color:#52525b;margin-top:2px">${placements.length} placements &middot; ${totalPlayers} players</div>
        </div>
        <span class="badge ok">Done</span>
      </div>
      <div class="card-body">
        <div class="placements">${rows}</div>
        <details class="json-toggle">
          <summary>Raw JSON</summary>
          <pre>${escHtml(JSON.stringify(result, null, 2))}</pre>
        </details>
      </div>`;
  }

  function renderError(item, msg) {
    item.cardEl.className = 'result-card error-card';
    item.cardEl.innerHTML = `
      <div class="card-header">
        <img src="${item.url}" />
        <span class="filename">${escHtml(item.file.name)}</span>
        <span class="badge err">Error</span>
      </div>
      <div class="card-body">
        <p class="error-msg">${escHtml(msg)}</p>
      </div>`;
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


@app.route("/ocr", methods=["POST"])
def ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    data = file.read()
    mime = file.mimetype or "image/jpeg"
    b64  = base64.b64encode(data).decode("utf-8")

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

    try:
        resp = http_requests.post(url, json=payload, timeout=90)
        resp.raise_for_status()
    except http_requests.HTTPError as e:
        return jsonify({"error": f"Gemini API error {e.response.status_code}: {e.response.text[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    raw  = resp.json()
    text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if model ignores responseMimeType
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse Gemini response: {e}\n\nRaw: {text[:400]}"}), 500

    return jsonify({"result": result})


if __name__ == "__main__":
    print("\n  AFC OCR Tester running at  http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=False)
