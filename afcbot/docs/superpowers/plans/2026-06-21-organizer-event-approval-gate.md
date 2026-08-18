# Organizer-Event Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold organizer-created events for admin approval in the mods channel (persistent Approve/Reject buttons) before they are announced publicly; AFC-run events keep auto-announcing.

**Architecture:** Insert a gate inside `event_poll_loop`. New organizer events (non-empty `organization_name`) are posted as a preview to the mods channel with a persistent `discord.ui.View`; the public announcement only fires when an authorized admin clicks Approve. State (pending approvals, rejected ids) is persisted to `BASE_DIR` JSON files so it survives restarts, and the status-change loop is taught to skip pending/rejected events so nothing leaks early.

**Tech Stack:** Python, py-cord (`discord.Client`, `discord.ui.View`, persistent views via `bot.add_view`), single-file `bot.py`.

## Global Constraints

- Single file: all changes in `bot.py`. No new modules.
- After **every** task: `python -m py_compile bot.py` must pass.
- py-cord button callback signature is `async def cb(self, button, interaction)` (button first, interaction second). Do not swap.
- Persistent view requires every item to have a static `custom_id` and the view `timeout=None`.
- Reuse `build_event_embed`; never duplicate embed-building logic.
- Do **not** change the `event_type == "external"` skip, nor status-change behavior for AFC / approved events.
- New state files live in `BASE_DIR`, JSON, matching the existing `seen_*.json` helper pattern.
- Git commits: **no `Co-authored-by: Claude` trailer, no AI attribution** (hook-enforced). Imperative subject < 72 chars.
- No `npm`.
- Approver role set: `EVENT_APPROVAL_ROLES = set(ANNOUNCE_ROLES + SUPPORT_ROLES)`.

---

### Task 1: Config constants, state persistence helpers, organizer predicate

**Files:**
- Modify: `bot.py` config block (`PENDING_EVENT_APPROVALS_FILE`/`REJECTED_EVENT_IDS_FILE` next to `SEEN_*` ~line 147; `EVENT_APPROVAL_ROLES` next to `TRANSCRIPTION_ROLES` ~line 156)
- Modify: `bot.py` near `load_seen_events` (~line 432) — add module globals, load/save helpers, `is_organizer_event`, `_pending_event_ids`
- Test: throwaway `_verify_approval.py` (created, run, deleted — never committed)

**Interfaces:**
- Produces:
  - `PENDING_EVENT_APPROVALS_FILE: str`, `REJECTED_EVENT_IDS_FILE: str`, `EVENT_APPROVAL_ROLES: set[int]`
  - `_pending_event_approvals: dict[str, dict]` (message_id → event), `_rejected_event_ids: set[str]`
  - `load_pending_event_approvals() -> dict`, `save_pending_event_approvals() -> None`
  - `load_rejected_event_ids() -> set`, `save_rejected_event_ids() -> None`
  - `is_organizer_event(event: dict) -> bool`
  - `_pending_event_ids() -> set[str]`

- [ ] **Step 1: Add the config constants**

In `bot.py`, after `SEEN_BAN_ACTIVITIES_FILE = ...` (~line 147) add:

```python
# Organizer-event approval gate — persisted state
PENDING_EVENT_APPROVALS_FILE = os.path.join(BASE_DIR, "pending_event_approvals.json")
REJECTED_EVENT_IDS_FILE      = os.path.join(BASE_DIR, "rejected_event_ids.json")
```

After `TRANSCRIPTION_ROLES = ...` (~line 156) add:

```python
# Roles allowed to approve/reject organizer-event announcements (mirrors transcription perms)
EVENT_APPROVAL_ROLES = set(ANNOUNCE_ROLES + SUPPORT_ROLES)
```

- [ ] **Step 2: Add globals, helpers, and predicate**

In `bot.py`, immediately after `save_seen_events` (~line 447) add:

```python
# ── Organizer-event approval state ────────────────────────────────────────────
# message_id(str) -> event dict awaiting an admin's approval in the mods channel.
_pending_event_approvals: dict[str, dict] = {}
# event_id(str) set — organizer events an admin rejected; never auto-posted again.
_rejected_event_ids: set[str] = set()


def load_pending_event_approvals() -> dict:
    if os.path.exists(PENDING_EVENT_APPROVALS_FILE):
        try:
            with open(PENDING_EVENT_APPROVALS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items()}
        except Exception as e:
            print(f"⚠️  Could not load pending_event_approvals.json: {e}")
    return {}


def save_pending_event_approvals():
    try:
        with open(PENDING_EVENT_APPROVALS_FILE, "w", encoding="utf-8") as f:
            json.dump(_pending_event_approvals, f)
    except Exception as e:
        print(f"⚠️  Could not save pending_event_approvals.json: {e}")


def load_rejected_event_ids() -> set:
    if os.path.exists(REJECTED_EVENT_IDS_FILE):
        try:
            with open(REJECTED_EVENT_IDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {str(x) for x in data}
        except Exception as e:
            print(f"⚠️  Could not load rejected_event_ids.json: {e}")
    return set()


def save_rejected_event_ids():
    try:
        with open(REJECTED_EVENT_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_rejected_event_ids), f)
    except Exception as e:
        print(f"⚠️  Could not save rejected_event_ids.json: {e}")


def _pending_event_ids() -> set:
    """event_ids currently awaiting approval (derived from pending payloads)."""
    return {str(ev.get("event_id")) for ev in _pending_event_approvals.values()}


def is_organizer_event(event: dict) -> bool:
    """True if a partner organization created this event (vs an AFC-run event).

    AFC-run events carry no organization_name (build_event_embed falls back to
    'African Freefire Community'); a non-empty org name that isn't AFC itself
    marks a partner-organizer event that must be approved before announcing.
    """
    org = (event.get("organization_name") or "").strip()
    if not org:
        return False
    return org.lower() != "african freefire community"
```

- [ ] **Step 3: Write the failing verification script**

Create `_verify_approval.py` in the repo root:

```python
import os, sys, tempfile, importlib
import bot

# --- is_organizer_event ---
assert bot.is_organizer_event({"organization_name": "Team Phoenix"}) is True
assert bot.is_organizer_event({"organization_name": ""}) is False
assert bot.is_organizer_event({"organization_name": None}) is False
assert bot.is_organizer_event({}) is False
assert bot.is_organizer_event({"organization_name": "  African Freefire Community "}) is False
assert bot.is_organizer_event({"organization_name": "african freefire community"}) is False

# --- pending approvals round-trip ---
tmp = tempfile.mkdtemp()
bot.PENDING_EVENT_APPROVALS_FILE = os.path.join(tmp, "pending.json")
bot.REJECTED_EVENT_IDS_FILE = os.path.join(tmp, "rejected.json")

bot._pending_event_approvals = {"123": {"event_id": 7, "event_name": "Cup"}}
bot.save_pending_event_approvals()
assert bot.load_pending_event_approvals() == {"123": {"event_id": 7, "event_name": "Cup"}}
assert bot._pending_event_ids() == {"7"}

# --- rejected ids round-trip ---
bot._rejected_event_ids = {"7", "9"}
bot.save_rejected_event_ids()
assert bot.load_rejected_event_ids() == {"7", "9"}

# --- missing files default empty ---
bot.PENDING_EVENT_APPROVALS_FILE = os.path.join(tmp, "nope.json")
bot.REJECTED_EVENT_IDS_FILE = os.path.join(tmp, "nope2.json")
assert bot.load_pending_event_approvals() == {}
assert bot.load_rejected_event_ids() == set()

print("OK")
```

- [ ] **Step 4: Run it to verify it fails (functions not yet defined → AttributeError)**

Run: `python _verify_approval.py`
Expected (BEFORE Steps 1-2 applied): `AttributeError: module 'bot' has no attribute 'is_organizer_event'`. (If you applied Steps 1-2 first, instead confirm Step 6 passes.)

- [ ] **Step 5: Syntax check**

Run: `python -m py_compile bot.py`
Expected: no output (success).

- [ ] **Step 6: Run the verification script — expect pass**

Run: `python _verify_approval.py`
Expected: prints `OK`.

- [ ] **Step 7: Delete the throwaway script and commit**

```bash
rm _verify_approval.py
git add bot.py
git commit -m "Add organizer-event approval state: config, persistence, predicate"
```

---

### Task 2: Extract `announce_event_public` (DRY — used by both auto and approved paths)

**Files:**
- Modify: `bot.py` — add `announce_event_public` near `build_event_embed` (~line 503); replace the inline send block in `event_poll_loop` (~lines 607-624)

**Interfaces:**
- Consumes: `build_event_embed` (Task already in repo)
- Produces: `async def announce_event_public(event: dict) -> None` — posts the event's embed to the correct public channel with the existing ping rules.

- [ ] **Step 1: Add the helper**

After `build_event_embed` returns (~line 503, before `_load_event_statuses`) add:

```python
async def announce_event_public(event: dict):
    """Post an event's announcement embed to its public tournament/scrim channel,
    with the same @everyone (tournament) / scrim-role ping rules used since launch."""
    embed, ping = await build_event_embed(event)
    is_scrim = event.get("competition_type", "").lower() == "scrims"
    ch_id = SCRIM_ANNOUNCEMENT_CHANNEL_ID if is_scrim else TOURNAMENT_ANNOUNCEMENT_CHANNEL_ID
    channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    everyone_ping = "@everyone" if not is_scrim else ""
    content = " ".join(filter(None, [everyone_ping, ping]))
    await channel.send(content=content, embed=embed)
```

- [ ] **Step 2: Replace the inline new-event send block**

In `event_poll_loop`, replace the whole `for event in reversed(new_events):` body (~lines 607-624) with:

```python
            for event in reversed(new_events):
                eid = str(event["event_id"])
                try:
                    await announce_event_public(event)
                    seen.add(eid)
                    event_statuses[eid] = event.get("event_status", "")  # seed so no status-change fires
                    print(f"🎮  Announced event: {event.get('event_name')}")
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"⚠️  Failed to post event {eid}: {e}")
```

(The organizer-gate branch is added in Task 4; this step is a pure behavior-preserving refactor.)

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile bot.py`
Expected: no output.

- [ ] **Step 4: Behavior-preservation check (reason + diff)**

Confirm the new `announce_event_public` builds identical content to the old inline block: tournaments get `@everyone`, scrims get the scrim ping, same channel selection, same `embed`. `git diff bot.py` should show only the extraction — no logic change.

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "Extract announce_event_public from event poll loop (no behavior change)"
```

---

### Task 3: `EventApprovalView` + `post_event_for_approval`

**Files:**
- Modify: `bot.py` — add the view class and the approval-poster after `announce_event_public` (~line 515)

**Interfaces:**
- Consumes: `EVENT_APPROVAL_ROLES`, `_pending_event_approvals`, `_rejected_event_ids`, `save_pending_event_approvals`, `save_rejected_event_ids`, `announce_event_public`, `build_event_embed`, `MODS_CHANNEL_ID`
- Produces:
  - `class EventApprovalView(discord.ui.View)` with buttons `custom_id="afc_event_approve"` / `"afc_event_reject"`
  - `async def post_event_for_approval(event: dict) -> None`

- [ ] **Step 1: Add the view class**

After `announce_event_public` add:

```python
class EventApprovalView(discord.ui.View):
    """Persistent Approve/Reject buttons for organizer-event previews in the mods
    channel. One instance is registered globally in on_ready; it resolves the
    pending event by the message id the buttons live on."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="afc_event_approve")
    async def approve(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle(interaction, approved=True)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="afc_event_reject")
    async def reject(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle(interaction, approved=False)

    async def _handle(self, interaction: discord.Interaction, approved: bool):
        member = interaction.user
        roles = getattr(member, "roles", [])
        if not any(getattr(r, "id", None) in EVENT_APPROVAL_ROLES for r in roles):
            await interaction.response.send_message(
                "⛔ You're not authorized to approve event announcements.", ephemeral=True
            )
            return

        mid = str(interaction.message.id)
        event = _pending_event_approvals.pop(mid, None)
        if event is None:
            await interaction.response.send_message(
                "⚠️ This approval was already handled or has expired.", ephemeral=True
            )
            return
        save_pending_event_approvals()

        if approved:
            try:
                await announce_event_public(event)
            except Exception as e:
                _pending_event_approvals[mid] = event   # re-queue so approval isn't lost
                save_pending_event_approvals()
                await interaction.response.send_message(
                    f"⚠️ Couldn't post the announcement: {e}", ephemeral=True
                )
                return
            note = f"✅ **Approved** by {member.mention} — announcement posted."
            print(f"✅  Event approved by {member}: {event.get('event_name')}")
        else:
            _rejected_event_ids.add(str(event.get("event_id")))
            save_rejected_event_ids()
            note = f"❌ **Rejected** by {member.mention} — not announced."
            print(f"❌  Event rejected by {member}: {event.get('event_name')}")

        # Single interaction response: rewrite the preview content and drop the buttons.
        await interaction.response.edit_message(content=note, view=None)
```

- [ ] **Step 2: Add the approval-poster**

After the view class add:

```python
async def post_event_for_approval(event: dict):
    """Send an organizer event to the mods channel for admin approval instead of
    announcing it publicly. Raises on failure so the poll loop can retry."""
    channel = bot.get_channel(MODS_CHANNEL_ID) or await bot.fetch_channel(MODS_CHANNEL_ID)
    embed, _ping = await build_event_embed(event)
    is_scrim  = event.get("competition_type", "").lower() == "scrims"
    target    = "scrim" if is_scrim else "tournament"
    organizer = event.get("organization_name") or "an organizer"
    header = (
        f"🕓 **PENDING APPROVAL** — new {target} from **{organizer}**.\n"
        f"Approve to announce it publicly, or reject to discard."
    )
    msg = await channel.send(content=header, embed=embed, view=EventApprovalView())
    _pending_event_approvals[str(msg.id)] = event
    save_pending_event_approvals()
    print(f"🕓  Event sent for approval: {event.get('event_name')} (msg {msg.id})")
```

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile bot.py`
Expected: no output.

- [ ] **Step 4: Import-time sanity check (view constructs, custom_ids correct)**

Create throwaway `_verify_view.py`:

```python
import bot
v = bot.EventApprovalView()
ids = sorted(c.custom_id for c in v.children)
assert ids == ["afc_event_approve", "afc_event_reject"], ids
assert v.timeout is None
print("OK")
```

Run: `python _verify_view.py` → expect `OK`. Then `rm _verify_view.py`.

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "Add EventApprovalView and mods-channel approval poster"
```

---

### Task 4: Wire the gate into `event_poll_loop`

**Files:**
- Modify: `bot.py` — `event_poll_loop` new-event branch (route organizer events) and status-change branch (skip pending/rejected)

**Interfaces:**
- Consumes: `is_organizer_event`, `post_event_for_approval`, `announce_event_public`, `_pending_event_ids`, `_rejected_event_ids`

- [ ] **Step 1: Route organizer events to approval in the new-event loop**

Replace the new-event `for` body (the one written in Task 2 Step 2) with:

```python
            for event in reversed(new_events):
                eid = str(event["event_id"])
                try:
                    if is_organizer_event(event):
                        await post_event_for_approval(event)
                    else:
                        await announce_event_public(event)
                        print(f"🎮  Announced event: {event.get('event_name')}")
                    seen.add(eid)
                    event_statuses[eid] = event.get("event_status", "")  # seed: no status-change fires
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"⚠️  Failed to handle new event {eid}: {e}")
```

Note: `seen.add` runs only after the organizer preview (or AFC post) succeeded — a failed mods-channel post leaves the event unseen so it retries next poll.

- [ ] **Step 2: Skip pending + rejected events in the status-change branch**

In the `for event in events:` status-change loop, just after the external-skip and `eid = str(event.get("event_id"))` line (~line 635), add:

```python
                # Never announce status changes for events still awaiting approval or
                # already rejected — they were not (and may never be) publicly posted.
                if eid in _rejected_event_ids or eid in _pending_event_ids():
                    continue
```

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile bot.py`
Expected: no output.

- [ ] **Step 4: Logic walkthrough (reason against spec)**

Confirm on paper:
- Organizer new event → `post_event_for_approval` only (no public post), `seen` + status seeded.
- AFC new event → `announce_event_public` (unchanged), `seen` + status seeded.
- Pending event's status flips → status branch `continue`s (no leak).
- Rejected event's status flips → status branch `continue`s.
- Approved event (no longer pending, not rejected) → status changes announce normally.

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "Gate organizer events behind approval in event poll loop"
```

---

### Task 5: Register persistent view + reload state in `on_ready`

**Files:**
- Modify: `bot.py` — `on_ready` (~line 2719, before the `create_task` calls)

**Interfaces:**
- Consumes: `EventApprovalView`, `load_pending_event_approvals`, `load_rejected_event_ids`

- [ ] **Step 1: Reload state and register the persistent view**

In `on_ready`, after the `_cached_events` pre-fetch block and before `bot.loop.create_task(auto_purge_loop())` (~line 2726), add:

```python
    # Restore organizer-event approval state and bind the persistent buttons so
    # previews posted before this restart keep working.
    global _pending_event_approvals, _rejected_event_ids
    _pending_event_approvals = load_pending_event_approvals()
    _rejected_event_ids = load_rejected_event_ids()
    bot.add_view(EventApprovalView())
    print(f"🕓  Approval gate: {len(_pending_event_approvals)} pending, {len(_rejected_event_ids)} rejected")
```

- [ ] **Step 2: Syntax check**

Run: `python -m py_compile bot.py`
Expected: no output.

- [ ] **Step 3: Verify global decl doesn't clash**

`on_ready` already declares `global _cached_events`. Confirm the new `global _pending_event_approvals, _rejected_event_ids` is a separate statement and both globals exist at module scope (Task 1). `python -m py_compile bot.py` passing covers syntax; visually confirm no duplicate/contradictory global lines.

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "Register approval view and reload approval state on ready"
```

---

### Task 6: End-to-end verification + adversarial review

**Files:**
- No production changes unless review finds a defect.

- [ ] **Step 1: Full syntax check**

Run: `python -m py_compile bot.py`
Expected: no output.

- [ ] **Step 2: Simulated flow harness (throwaway)**

Create `_verify_e2e.py` that monkeypatches the Discord touch-points and drives one organizer event through approval and one through rejection, asserting the public-post helper fires only on approve. Use `unittest.mock`:

```python
import asyncio, types, bot
from unittest.mock import AsyncMock, MagicMock

# Stub channel.send to capture posts
posted = []
async def fake_announce(event): posted.append(event["event_name"])
bot.announce_event_public = fake_announce

# Build a fake interaction with an authorized member
role = MagicMock(); role.id = next(iter(bot.EVENT_APPROVAL_ROLES))
member = MagicMock(); member.roles = [role]; member.mention = "@admin"
inter = MagicMock(); inter.user = member
inter.message = MagicMock(); inter.message.id = 555
inter.response = MagicMock()
inter.response.edit_message = AsyncMock()
inter.response.send_message = AsyncMock()

bot._pending_event_approvals = {"555": {"event_id": 1, "event_name": "OrgCup", "organization_name": "Org"}}

view = bot.EventApprovalView()
asyncio.run(view._handle(inter, approved=True))
assert posted == ["OrgCup"], posted
assert "555" not in bot._pending_event_approvals
inter.response.edit_message.assert_awaited_once()
print("approve OK")

# Reject path
bot._pending_event_approvals = {"556": {"event_id": 2, "event_name": "BadCup", "organization_name": "Org"}}
inter.message.id = 556
asyncio.run(view._handle(inter, approved=False))
assert "2" in bot._rejected_event_ids
assert posted == ["OrgCup"]   # no new public post on reject
print("reject OK")

# Unauthorized
member.roles = []
bot._pending_event_approvals = {"557": {"event_id": 3, "event_name": "X"}}
inter.message.id = 557
asyncio.run(view._handle(inter, approved=True))
assert "557" in bot._pending_event_approvals   # untouched
print("authz OK")
print("OK")
```

Run: `python _verify_e2e.py` → expect `approve OK / reject OK / authz OK / OK`. Then `rm _verify_e2e.py`.

> Note: this repo has **no pytest suite** by design (single-file bot). These throwaway harnesses are the verification surface, alongside `py_compile` and the live-run smoke check.

- [ ] **Step 3: Live smoke (optional but preferred)**

If a `.env` with valid `DISCORD_TOKEN`/`OPENAI_API_KEY` is available, run `python bot.py`, confirm the startup log prints `🕓  Approval gate: 0 pending, 0 rejected` and no traceback. Stop after confirming online.

- [ ] **Step 4: Adversarial multi-agent review (ultracode)**

Run a review workflow over the diff focused on the high-risk seams: restart/persistence correctness, status-change leak paths, the persistent-view shared-instance concern, race conditions on double-click, and authorization bypass. Triage findings; fix any real defect and re-run Steps 1-2.

- [ ] **Step 5: Final commit (if review produced fixes)**

```bash
git add bot.py
git commit -m "Address review findings on approval gate"
```

---

## Self-Review

**Spec coverage:**
- Scope = organizer-only → Task 1 `is_organizer_event` + Task 4 routing. ✓
- Mods channel preview → Task 3 `post_event_for_approval`. ✓
- Approve/Reject persistent buttons → Task 3 `EventApprovalView` + Task 5 `bot.add_view`. ✓
- Durability across restart → Task 1 persistence + Task 5 reload/register. ✓
- No status-leak before/without approval → Task 4 Step 2 skip. ✓
- Status seeded at prompt time → Task 4 Step 1 `event_statuses[eid] = ...`. ✓
- Race + authz + final-reject → Task 3 `_handle`. ✓
- AFC events unchanged → Task 2 extraction + Task 4 else-branch. ✓

**Placeholder scan:** none — every code step shows complete code; commands have expected output.

**Type consistency:** `_pending_event_approvals` (dict[str,dict]) and `_rejected_event_ids` (set[str]) used consistently across Tasks 1/3/4/5; `is_organizer_event`, `announce_event_public`, `post_event_for_approval`, `_pending_event_ids` names identical at every reference; button custom_ids `afc_event_approve`/`afc_event_reject` match between Task 3 and the persistent-view requirement.
