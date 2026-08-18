# AFC Discord Bot 🔥🎮

> **This bot lives inside the AFC backend repo** (`AFC-B/afcbot/`) since 2026-08-18.
> It used to have a repository of its own. It runs as its own systemd service, `afc-bot`, in its
> own virtualenv at `afcbot/venv`; see `deploy/systemd/README.md` at the repo root. The management
> UI is the AFC admin's **Bot** page (`/a/bot`), which talks to the control API in `bot.py`.

The official AI-powered Discord bot for the **African Freefire Community (AFC)**.  
Powered by **GPT-4o** and trained on your website content.

---

## Features
- ✅ Responds when @mentioned in allowed channels
- ✅ GPT-4o AI replies with full AFC website knowledge
- ✅ Understands and replies in **Nigerian Pidgin English**
- ✅ Handles **threaded replies** (reads what user was replying to)
- ✅ Per-channel **conversation memory** (last 30 messages)
- ✅ **Document upload system** — update knowledge without restarting
- ✅ **Website scraper** — re-scrape AFC site anytime to stay current
- ✅ All 13 AFC channels pre-configured

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up environment variables
```bash
cp .env.example .env
```
Edit `.env` and fill in:
```
DISCORD_TOKEN=your_discord_bot_token_here
OPENAI_API_KEY=your_openai_api_key_here
```
> ⚠️ Never share your `.env` file or post API keys publicly.

#### Optional: backup AI provider (failover)
If OpenAI runs out of quota or gets rate-limited (HTTP 429 / `insufficient_quota`),
the bot can automatically fail over to a second, **OpenAI-compatible** AI provider
so it keeps answering instead of going dark. Works with Groq, OpenRouter, DeepSeek,
Together, a second OpenAI key, a local Ollama, etc. Leave these unset to disable
failover — the bot then shows a clean "AI temporarily unavailable" notice instead
of a raw error.
```
FALLBACK_API_KEY=your_backup_provider_key
FALLBACK_BASE_URL=https://api.groq.com/openai/v1   # the provider's OpenAI-compatible endpoint
FALLBACK_MODEL=llama-3.3-70b-versatile             # model for normal replies (optional; this is the default)
FALLBACK_MINI_MODEL=llama-3.3-70b-versatile        # model for the cheap classifier (optional; defaults to FALLBACK_MODEL)
```
> Groq has a free tier and an OpenAI-compatible endpoint, so it's the easiest drop-in backup.

### 3. Enable Discord Bot Intents
In the [Discord Developer Portal](https://discord.com/developers/applications):
- Go to your app → **Bot**
- Enable **Message Content Intent** under Privileged Gateway Intents
- Save changes

### 4. Invite the bot to your server
In the portal → **OAuth2 → URL Generator**:
- Scopes: `bot`
- Permissions: `Send Messages`, `Read Message History`, `View Channels`, `Read Messages`

### 5. Run the bot
```bash
python bot.py
```

---

## Updating the Bot's Knowledge

### Option A — Re-scrape the website
Run this whenever your website content changes:
```bash
python scrape_site.py
```
This automatically updates `knowledge_base.txt`. No bot restart needed.

### Option B — Upload a document
Add a PDF or TXT file to the bot's knowledge:
```bash
python upload_docs.py path/to/your/document.pdf
python upload_docs.py path/to/your/document.txt
```

List current documents:
```bash
python upload_docs.py
```

Remove a document:
```bash
python upload_docs.py --remove filename.txt
```

All uploaded files are stored in the `knowledge/` folder.  
The bot reads this folder live — **no restart required**.

---

## File Structure
```
afc_bot/
├── bot.py              ← Main bot (run this)
├── knowledge_base.txt  ← Auto-generated from website scrape
├── scrape_site.py      ← Re-scrape the AFC website
├── upload_docs.py      ← Upload new docs to bot knowledge
├── requirements.txt    ← Python dependencies
├── .env.example        ← Environment variable template
├── .env                ← Your secrets (never share this!)
└── knowledge/          ← Folder for uploaded extra documents
```

---

## Customising the Bot Personality
Edit the `build_system_prompt()` function in `bot.py` to change the bot's tone, add more rules, or restrict what it can answer.

---

## Configured Channels
The bot listens in these 13 channels:
- 920726991089598476
- 1327968058148524133
- 1014588126422904873
- 946321672015851570
- 1079786358840766554
- 1306928470802042931
- 1011289377055449178
- 1324442579265388644
- 1092544100072423435
- 920795335272579102
- 953326236950757446
- 1340452836495851713
- 955773076786798643

To add more channels, edit the `ALLOWED_CHANNELS` list in `bot.py`.
