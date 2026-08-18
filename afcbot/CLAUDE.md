# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot for the African Freefire Community (AFC). It listens in configured channels, classifies whether a message needs a reply, and answers using GPT-4o grounded in a file-based knowledge base. It also runs several background loops that announce new tournaments/scrims, news articles, and bans pulled from the AFC backend API.

The entire bot is a single ~3.7k-line file: `bot.py`. There is no framework, no module split, no test suite. Edits to behavior almost always happen in `bot.py` or in the knowledge files.

## Commands

```bash
# Run the bot locally
python bot.py

# Re-scrape the AFC website into knowledge_base.txt (no restart needed — bot reads from disk on every reply)
python scrape_site.py

# Add a document to the live knowledge folder
python upload_docs.py path/to/file.txt
python upload_docs.py path/to/file.pdf
python upload_docs.py                      # list current docs
python upload_docs.py --remove file.txt    # remove a doc

# Syntax-check after editing bot.py
python -m py_compile bot.py
```

Production runs as the systemd service `afc-bot`, defined in `deploy/systemd/afc-bot.service`
at the repo root, using its own virtualenv at `afcbot/venv`. (`Procfile` is kept only as a record of
how it was deployed before it moved into this repo on 2026-08-18.)

The knowledge refresh is a **celery beat task** (`afc_bot.tasks.refresh_bot_knowledge`, scheduled
in `afc/celery_config.py`) that re-scrapes every 3 hours and writes `knowledge_base.txt` to disk.
It replaced a GitHub Action that committed the file to the bot's old repository; here that would be
eight machine commits a day in the backend's history. **Do not edit `knowledge_base.txt` by hand** — your changes will be overwritten by the next scheduled scrape. Put curated content in `knowledge/` instead.

There are no tests, no linter config, and no build step.

## Architecture

### Single-file layout
`bot.py` is organized in clearly-marked sections (search for `# ──`):
- Config constants (channel IDs, role IDs, poll intervals, API base)
- Persistent history helpers (load/save/trim/purge `conversation_history.json`)
- Background loop: knowledge auto-scrape
- Background loop: news polling → news announcement channel
- Background loop: event polling → tournament/scrim channels (NEW events + status changes)
- Background loop: ban polling → ban/unban channels
- Knowledge base loader (`load_knowledge`, `load_staff_knowledge`)
- Time/event helpers (`_parse_event_datetime`, `compute_time_status`, `format_live_events`)
- System-prompt builder (`build_system_prompt`)
- OpenAI call wrappers (`ask_openai_text`, `ask_openai_with_image`, `transcribe_audio`)
- `send_support_redirect` (the human-escalation embed)
- Stage transcription (Whisper) flow
- `on_ready` (registers all background loops), `on_message`, `on_message_edit`
- `should_bot_respond` classifier
- `_handle_message` (the main reply pipeline)

When adding behavior, find the existing section that owns it and edit there — do not split into new files.

### Two-stage reply pipeline
1. **Classifier (`should_bot_respond`)** — `gpt-4o-mini`, 5-token reply, defaults to YES on any error so the bot never silently ignores someone. Reads any attached image alongside the text. The criteria are deliberately permissive: implicit help requests, statements about platform problems, typos, and Discord replies to other users all qualify as YES.
2. **Reply (`ask_openai_text` / `ask_openai_with_image`)** — `gpt-4o`, system prompt assembled fresh each call by `build_system_prompt()`. The reply may contain `---SUPPORT_REDIRECT---`, which the wrapper strips and signals to the caller via a `needs_support` boolean. Caller then calls `send_support_redirect()` which posts a separate embed pointing at `<#SUPPORT_CHANNEL_ID>` and pings the support roles.

### System prompt is the product
`build_system_prompt()` (`bot.py:1033`) is where most "behavior" lives. It interpolates:
- Loaded knowledge (`load_knowledge()` — public; `load_staff_knowledge()` — only when `is_staff=True`)
- `format_live_events()` — a snapshot of `_cached_events`, refreshed every `EVENT_POLL_INTERVAL_SECS` (120s) by `event_poll_loop`
- Constants like `AFC_DISCORD_INVITE` and `SUPPORT_CHANNEL_ID`

When you're asked "fix the bot's behavior" for replies, the answer is almost always to edit the prompt in `build_system_prompt()`, not to write code. Rules in that prompt are authoritative — they have history (e.g. the Discord-link rule and the support-channel rule both exist because GPT was getting them wrong).

### Knowledge base layering
Three sources, all loaded fresh on every reply (no restart needed for content updates):
1. `knowledge_base.txt` — auto-scraped from africanfreefirecommunity.com. **Auto-overwritten by GitHub Actions and `auto_scrape_loop`. Do not hand-edit.**
2. `knowledge/` — curated docs (.txt, .pdf, .docx, .xlsx). The loader reads PDFs via `pdfplumber`, Word via `mammoth`, Excel via `openpyxl`. This is the right place for hand-written knowledge.
3. `knowledge_staff/` — staff-only knowledge, only injected when the message author has a role in `STAFF_KNOWLEDGE_ROLES`. Includes a hard rule in the prompt to never reveal it to non-staff.

### Background loops, polling intervals, and seen-state
Five background tasks are created in `on_ready` and run forever:
- `auto_purge_loop` — purge conversation history older than 24h
- `auto_scrape_loop` — re-scrape website every 6h
- `news_poll_loop` — poll `/auth/get-all-news/`, post new articles to `NEWS_ANNOUNCEMENT_CHANNEL_ID`
- `event_poll_loop` — poll `/events/get-all-events/`, post new events AND announce status changes (the `status` field flipping from e.g. `pending` → `live`). Also refreshes `_cached_events` for the system prompt.
- `ban_poll_loop` — poll `/auth/get-admin-activities/`, post ban/unban embeds

Each loop deduplicates against a `seen_*.json` file in `BASE_DIR` so restarts don't re-spam old items. On first boot, an empty seen-set is **seeded with the current snapshot** rather than left empty — this prevents the bot from announcing every existing item the first time it runs.

### Event-time interpretation pitfall
The AFC API's `event_date` / `event_time` is **not a reliable match-start time** — it often represents the registration deadline or just a listed date. `compute_time_status()` therefore never returns "live" or "ended" from time alone; it only describes whether the listed date is `upcoming`, `starting_soon`, `date_passed`, or `unknown`. The **only** source of truth for live/ended state is the `status` field from the backend. There used to be a "time-derived auto-announcement" loop that posted "Tournament likely ENDED" embeds when the listed date passed — it was removed because it was spamming false ended announcements. Do not bring it back.

### Channel allowlist + auto-reply
`is_allowed_channel()` admits a channel if it's in `ALLOWED_CHANNELS` or under a category in `ALLOWED_CATEGORIES`. Within an allowed channel, the bot will reply to **any** non-trivial message that the classifier says YES to — `@mention` is not required. Mentions still bypass the classifier and always reply.

### Conversation history
`history` is `{channel_id: {messages: [...], last_updated: ts}}`, persisted to `conversation_history.json`. Capped at `MAX_HISTORY` (30) messages per channel and auto-purged after `HISTORY_TTL_SECS` (24h). All paths are computed from `BASE_DIR = os.path.dirname(os.path.abspath(__file__))` to avoid Windows permission issues.

### Stage transcription
`on_stage_instance_create` prompts mods in `MODS_CHANNEL_ID` to ask if they want the bot to join and transcribe a stage channel. If yes, it joins via `discord.sinks.WaveSink`, records, and posts the Whisper transcript when stopped. Only members with a role in `TRANSCRIPTION_ROLES` can confirm.

## Things to know before editing

- **Don't touch `knowledge_base.txt` by hand.** It's regenerated on a schedule. Hand-curated content goes in `knowledge/`.
- **Most "fix the bot" requests are prompt edits**, not code edits. Look in `build_system_prompt()` first.
- **The classifier defaults to YES on error** — that's intentional, don't change it. Better to over-respond than miss a real question.
- **Channel and role IDs are hardcoded constants** at the top of `bot.py`. There is no `.env`-based config for them. If you're adding a new channel, add it to the appropriate constant list (`ALLOWED_CHANNELS`, `AUTO_REPLY_CHANNELS`, etc.) and the bot will pick it up after restart.
- **`AFC_DISCORD_INVITE` is hardcoded** specifically because GPT used to hallucinate fake invite codes. The system prompt has a CRITICAL rule against ever using a different Discord URL. Don't loosen that rule.
- **Background loops swallow exceptions** at the top level so a single bad poll doesn't kill the loop. When debugging poll failures, look for the `⚠️` print lines in stdout/Heroku logs.
- **`needs_support` flows through the reply tuple**, not via raised exceptions. If you add new code paths that call `ask_openai_text`, remember to handle both elements of the returned tuple and call `send_support_redirect()` when the flag is true.
- **The "support channel" has two distinct mechanisms:** inline mention in the reply (use generously, any time the bot can't fully resolve) vs. the hard `---SUPPORT_REDIRECT---` marker (reserved for cases that need admin action — pings support roles). Both are documented in the system prompt; don't conflate them.

## Environment

Required env vars (loaded via `python-dotenv` from `.env`):
- `DISCORD_TOKEN`
- `OPENAI_API_KEY`

Discord bot needs the **Message Content Intent** enabled in the Discord Developer Portal.

---

# Agent Development Kit — Project Constitution

This repo is structured on the five-layer **Agent Development Kit**. The full blueprint lives in [`agent-development-kit.md`](agent-development-kit.md) — that file is canonical; read it for the complete model, the worked examples, and the full 37-item best-practices list. The rules below are the always-on subset, loaded every session, and are non-negotiable.

## The five layers, as instantiated in this repo

| Layer | Role | Where it lives here |
|---|---|---|
| **L1 CLAUDE.md** | Memory — sets the rules | this file + `agent-development-kit.md` |
| **L2 Skills** | Knowledge — auto-invoked expertise | `.claude/skills/` — `editing-system-prompt`, `managing-knowledge-base`, `bot-background-loops`, `deploying-the-bot` |
| **L3 Hooks** | Guardrails — deterministic shell | `.claude/hooks/*.sh`, wired in `.claude/settings.json` |
| **L4 Subagents** | Delegation — isolated context | `.claude/agents/` — `code-reviewer`, `prompt-auditor`, `bot-explorer` |
| **L5 Plugin** | Distribution — one-step install | `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` |

See [`.claude/README.md`](.claude/README.md) for the repo map of what each artifact does and how they wire together.

## Verification & handoff rules (mandatory before any handoff)

1. **Test fully and exhaustively.** Do not assume a change works. Cover the main path, edge cases, and failure cases before claiming done.
2. **Verify from the user's perspective.** This bot has **no browser UI** — the user surface is the running bot. After a behavior change, exercise the equivalent surface: `python -m py_compile bot.py` must pass, and where feasible run `python bot.py` and walk the affected flow (classifier → reply → support redirect; or the relevant background loop) before handing back. For prompt changes, reason through the exact message that previously misbehaved.
3. **For design/content work, compare against the approved reference.** Knowledge and prompt text is the product here — diff your wording against the existing authoritative rules (the hard rules below); don't loosen them.
4. **Iterate until it matches.** If verification shows a gap, fix and re-verify against the exact broken path until the symptom is gone.
5. **Only then hand over.** Deliver only after verification passes. State what was tested and how it was confirmed.
6. **Never add Claude as a git co-author.** No `Co-authored-by: Claude` trailer, no AI attribution in commit messages or PR authorship. Commits are authored solely by the user. (Enforced deterministically by `.claude/hooks/block-ai-coauthor.sh`.)

## Truth & accuracy rules

1. **Uncertainty.** If not fully certain, say so ("I am not certain, but…"). Never state guesses as facts.
2. **Sources.** Don't invent titles, authors, URLs, or references. If no verifiable source exists, say so.
3. **Statistics.** Flag any number you're not 100% sure of; say "approximately" and recommend verifying from a primary source.
4. **Recent events.** Flag topics that may have changed since the knowledge cutoff.
5. **People & quotes.** Never attribute a quote without certainty.
6. **Code & technical.** Never invent function names, library methods, or API syntax. If unsure a function/constant exists, verify it in `bot.py` or current docs first. (This repo's real symbols: `build_system_prompt` @ `bot.py:1045`, `should_bot_respond` @ `2367`, `ask_openai_text` @ `1423`, `event_poll_loop` @ `544`, etc. — grep before referencing.)
7. **Logic gaps.** Don't fill missing context with assumptions. If anything is unclear, stop and ask before proceeding.

## Best practices (binding subset — full 37 in `agent-development-kit.md`)

- **Simplicity first / surgical changes.** Minimum code that solves the problem. Touch only what the request requires. This is a single-file bot — find the section that owns the behavior and edit there; do not split into new files.
- **No suppressed errors.** Don't silence exceptions silently. The background loops intentionally swallow top-level exceptions and print `⚠️` lines — that's the existing pattern; match it, don't add silent bare-`except: pass`.
- **Least privilege & no secrets in code.** `DISCORD_TOKEN` / `OPENAI_API_KEY` live only in `.env` (never committed, never logged). Channel/role IDs are hardcoded constants by design.
- **Validate inputs / never log sensitive data.** Don't log tokens, keys, or full user PII.
- **Git discipline.** Atomic commits, imperative subject < 72 chars, body explains *why*. Use `gh` for PRs. No AI co-author (rule 6 above). Default workflow: branch + PR; confirm before pushing `main`.
- **No `npm`.** Machine policy — use `pnpm`/`bun`. (Enforced by `.claude/hooks/block-npm.sh`.)

## Resource discovery (discover before you ask; never hardcode)

1. **Check what exists first.** Before inventing a tool/skill/agent, check installed skills (`.claude/skills/`, `~/.claude/skills/`), connected MCP servers, available subagents (`.claude/agents/`), and repo scripts.
2. **Match by description, not name.** Pick the skill/agent/tool whose description semantically fits the task; prefer the most specific.
3. **Prefer the closest layer.** Project-local skill > global skill > new plugin. Use an existing slash-command before writing custom code.
4. **Confirm before installing anything new.** If nothing fits, report what was searched and propose — don't auto-add a marketplace/plugin/MCP server.
5. **Surface the choice.** Name which skill/agent/tool you picked and why, in one line.
6. **No cached resource lists / say so when nothing matches.** Re-check each session; if discovery finds nothing relevant, say so plainly and ask how to proceed.

---

## 🛑 HARD RULE - Design: no hairline borders, no glow (owner, 2026-08-17)

Two bans. Absolute. Every project, every framework, every component. Applies to code I write AND designs I propose.

### Ban 1 - No hairline / outlined anything

Never build structure out of 1px strokes. Banned shapes:

- **outlined card** - thin line rectangle drawn around content
- **outlined pill / chip** - filter chips with a ring (`All games`, `Streams`, `Teams`, ...)
- **divider / rule** - line between rows, list items, or sections
- **dashed placeholder box** - dashed outline empty state ("No events match your filters.")
- any empty state or section that is just a thin-line rectangle with centered text

Grep-level ban (CSS, Tailwind, RN, SwiftUI, Flutter):
`border`, `border-t|b|l|r`, `border-1`, `1px solid`, `border-dashed`, `divide-x`, `divide-y`, `ring-1`, `ring-2`, `outline: 1px`, `<hr>`, `Divider`, `BorderSide`, `.border(...)`, `stroke` on container frames.

Build hierarchy with **surface + space**, not lines:

| Instead of | Use |
|---|---|
| outlined card | filled surface, bg one step off the page bg, radius 12-16px, no stroke |
| outlined chip | filled chip (muted bg). Selected = stronger fill + text color. Never a ring |
| divider line | whitespace, or a background step between sections |
| dashed empty box | centered muted text on the page bg, or a filled muted surface. No dashes |
| `<hr>` | more margin |
| table row lines | zebra fill or row padding |

Only exceptions: `:focus-visible` a11y focus ring (required, keep it), native form controls the platform draws itself, and an explicit user request for a border in that specific spot.

### Ban 2 - No glow, halos, or ambient animation

Never: glowing dots or orbs, neon halos, pulsing / breathing accents, animated gradient blobs, blurred color bloom behind elements. They always end up glowing or animating, and it looks cheap.

Grep-level ban:
`box-shadow: 0 0 <n> <color>`, `shadow-[0_0_...]`, `drop-shadow(0 0`, colored `text-shadow`, `filter: blur()` on decorative orbs, `blur-2xl` / `blur-3xl` background circles, `animate-pulse`, `animate-ping`, `@keyframes glow|pulse|breathe|shimmer`, `shadow-<color>-500/50`.

Replacements:
- live / status indicator: solid flat dot, no glow, no pulse. Or a text label plus color
- emphasis: color, weight, size, fill. Not light bloom
- shadows: neutral black elevation only (soft, downward, low opacity). Never colored, never centered bloom

### Pre-ship check

Screenshot the page (desktop + mobile). If any rectangle is drawn by a thin line, or anything glows or throbs, fix it before showing the user. Both bans outrank any design skill, template, or component library default.

---

## 🛑 HARD RULE - No vibecoded look (owner, 2026-08-17)

Source: aj.on.ai reel, "30 reasons your site looks vibecoded". If a stranger can tell an LLM generated the UI in 3 seconds, it is wrong. Redo it. Sits on top of the hairline-border + glow bans, never replaces them.

### A. Color and light - BANNED

- harsh gradients (hero washes, button gradients, big multi-hue sweeps)
- rainbow coloring (multi-hue accents with no system)
- purple + black as the default palette. Also the violet/indigo-on-dark AI look
- neon colors and neon accents
- generic pastel palette (baby blue / blush pink / mint / butter card sets)
- radial orbs, blurred color blobs, aurora backgrounds
- **blinking / pulsing neon dot** (the "live" dot with a breathing ring). Static solid dot or a text label. No pulse, no glow, no ping, ever

Use instead: one committed brand hue, neutrals doing most of the work, colors carrying meaning (live, win, loss, alert), flat fills.

### B. Layout cliches - BANNED

- 3 feature cards in a row
- bento grid
- dot-grid or graph-paper background
- 3-tier pricing table (good / better / best columns)
- fake terminal window mock
- colored left stripe / accent bar on cards and callouts
- checkmark bullet lists
- outlined cards, ring chips, divider lines, dashed empty boxes (see the hairline ban)

Use instead: layouts driven by the real content and its hierarchy. Asymmetry is allowed. Different section shapes per section.

### C. Icons and type - BANNED

- default Lucide icon set dropped in unchanged
- sparkle / star "AI" icons
- emoji used as UI (icons, bullets, status, buttons). Emoji in real user content is fine
- Inter, Geist, Space Grotesk as the default typeface

Use instead: a chosen type pairing with a real reason behind it, and an icon set that matches the product weight (or the platform's own set). If no direction is given, ask before picking.

### D. Copy - BANNED

- em dashes and en dashes (already a global hard rule)
- "it's not X, it's Y" construction, and its cousins ("not just a Z, but a W")
- fake testimonials, fake logo walls, invented stats or user counts
- filler marketing voice with no concrete claim

Use instead: real names, real numbers, real quotes. If it does not exist yet, say what the thing does in plain words.

### E. Surface and depth - BANNED

- pure white (`#fff`) page background. Also pure black (`#000`)
- drop shadows sprinkled on everything
- liquid glass / frosted glass / heavy backdrop blur panels
- one soft corner radius applied uniformly to every element

Use instead: off-white or a real dark surface, a small radius scale used with intent (small elements small radius, big surfaces bigger), elevation only where something genuinely floats.

### F. Motion - BANNED

- hover animation on everything (lift, scale, glow, translate)
- animated arrows, marching chevrons, bouncing CTAs
- sparkle / shimmer / breathing effects

Use instead: instant state changes (fill, color, weight) for hover. Motion only for real feedback: opening, closing, loading, arriving. Respect `prefers-reduced-motion`.

### G. Missing pieces that scream vibecoded - REQUIRED

- **real product demo**: real screenshots, real data, real video. Not a mock frame with placeholder text
- **loading, empty, and error states**: skeletons or a real loader, a written empty state, a real error path. Every list and page
- **Terms of Service** and **Privacy Policy** pages that exist and are linked, on anything public facing
- real content everywhere. No lorem ipsum, no `Feature One`, no placeholder avatars shipped

### Pre-ship check

Ask: could this be any AI-generated landing page from this year? If yes, it is not done. Screenshot desktop + mobile, walk the list above, fix every hit before showing the user.
