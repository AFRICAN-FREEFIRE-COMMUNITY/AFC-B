# Organizer-Event Approval Gate — Design

**Date:** 2026-06-21
**Component:** `bot.py` — `event_poll_loop` and surrounding event-announcement code
**Status:** Approved (brainstorming), pending implementation plan

## Problem

Today `event_poll_loop` (bot.py:566) posts every newly detected non-external event **straight** to the
public tournament/scrim channel with an `@everyone` ping (tournaments) or scrim role ping. Events created
by partner **organizers** therefore go live with no human review. The AFC team wants organizer events held
for an **admin approval** in the mods channel before anything is posted publicly.

## Goals

- Insert an approval gate between detection and public announcement, **for organizer events only**.
- AFC-run events keep auto-announcing exactly as today (no behavior change).
- The gate must survive bot restarts — an approval may sit pending for hours or days.
- No public leak of an organizer event before approval (including via status-change embeds).

## Non-goals

- No change to the `event_type == "external"` skip — external events are still ignored entirely.
- No change to status-change announcements for AFC events or approved organizer events (stay automatic).
- No backend/API changes. We work only from the existing `/events/get-all-events/` payload.
- No re-prompt of rejected events. A reject is final.

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Scope | Only **organizer** events (non-empty `organization_name`). AFC events (empty org) auto-announce. |
| Approval channel | Mods channel — `MODS_CHANNEL_ID = 1324442579265388644`. |
| Mechanism | `discord.ui` **Approve / Reject buttons** (persistent view, survive restart). |
| Status changes | Stay **automatic** for AFC + approved organizer events. Only the initial NEW-event post is gated. |
| Approver roles | `ANNOUNCE_ROLES + SUPPORT_ROLES` (mirrors `TRANSCRIPTION_ROLES`). |
| Rejected events | Final — never re-prompted, never auto-posted (including status changes). |

## Discriminator: what is an "organizer event"

```python
def is_organizer_event(event: dict) -> bool:
    org = (event.get("organization_name") or "").strip()
    if not org:
        return False                      # AFC-run event → auto-announce
    return org.lower() != "african freefire community"  # AFC tagged with own name → still auto
```

`build_event_embed` already treats empty `organization_name` as "African Freefire Community", so this
predicate cleanly separates partner-org events from AFC's own.

## New flow

```
event_poll_loop detects new event  (str(event_id) not in seen, event_type != "external")
   ├─ NOT organizer  → post public immediately            [unchanged path]
   │                    seen.add(id); seed event_status
   └─ organizer      → post_event_for_approval():
                          • build the SAME announcement embed (reuse build_event_embed)
                          • wrap a "PENDING APPROVAL" header embed + the preview
                          • send to mods channel with EventApprovalView (Approve/Reject)
                          • pending_event_approvals[message_id] = event   (persist)
                          • seen.add(id)            ← so the loop never re-prompts
                          • seed event_statuses[id] ← so a status flip while pending fires nothing
                          • NOTHING posted publicly yet

Approve button (authorized role):
   • pop payload by interaction.message.id
   • post real embed to SCRIM/ TOURNAMENT channel with the exact same content+ping logic as today
   • edit preview → "✅ Approved by @user", disable both buttons
   • drop from pending_event_approvals (persist)

Reject button (authorized role):
   • pop payload
   • edit preview → "❌ Rejected by @user", disable both buttons
   • drop from pending_event_approvals; add event_id to rejected_event_ids (persist)
```

## Durability design

Approvals are long-lived, so the ephemeral `bot.wait_for` pattern used by stage transcription is unsuitable.

- **One module-level persistent view**, `EventApprovalView(discord.ui.View)` with `timeout=None` and two
  buttons carrying **static** `custom_id`s: `afc_event_approve`, `afc_event_reject`.
- Registered **once** in `on_ready` via `bot.add_view(EventApprovalView())` (no `message_id` arg) so it
  services every preview message — including those posted before the current process started.
- Button callbacks resolve the event via `interaction.message.id` → `pending_event_approvals.json`.
  py-cord delivers component interactions even for uncached messages, so old previews keep working.

### New persisted state (one file each, matching existing `SEEN_*` convention)

| File | Shape | Purpose |
|---|---|---|
| `pending_event_approvals.json` | `{ "<message_id>": <event dict> }` | Awaiting-approval previews; the stored event dict is the snapshot announced on approval. |
| `rejected_event_ids.json` | `[ "<event_id>", ... ]` | Events an admin rejected; permanently suppressed (incl. status changes). |

Load helpers reload both on boot. Save on every mutation (add pending, approve, reject), same as
`save_seen_events`. All paths from `BASE_DIR`.

## Gate-tightness rules

1. **No pre-approval leak via status loop.** The status-change section skips any event whose id is in the
   pending set (derived from `pending_event_approvals` payloads) or in `rejected_event_ids`. Only AFC events
   and approved organizer events can emit status-change embeds.
2. **Status seeded at prompt time.** When an organizer event is queued for approval, its `event_status` is
   recorded immediately, so a status flip during the pending window does not produce a stale change embed.
3. **Race safety.** Two admins clicking: the first pops the payload; the second finds none and gets an
   ephemeral "already handled" reply. Buttons are disabled after the first successful action.
4. **Authorization.** Only members holding a role in the approver set may act; others get an ephemeral
   "not authorized" reply and no state changes.
5. **Restart safety.** On boot, pending payloads + rejected ids reload; the persistent view re-binds; the
   poll loop's `seen` set already contains queued events so nothing is re-prompted or double-posted.

## Code placement (single file, surgical — no new modules)

| Location in `bot.py` | Change |
|---|---|
| Config block (near `SEEN_*` paths ~line 144, roles ~line 156) | `EVENT_APPROVAL_ROLES`, `PENDING_EVENT_APPROVALS_FILE`, `REJECTED_EVENT_IDS_FILE` |
| Near `build_event_embed` (~line 465) | `is_organizer_event()`, load/save helpers for the 2 new files, `EventApprovalView`, `post_event_for_approval()` |
| `event_poll_loop` new-event branch (~line 607) | route organizer events to `post_event_for_approval()`; keep AFC path unchanged |
| `event_poll_loop` status-change section (~line 631) | skip pending + rejected event ids |
| `on_ready` (~line 2726) | `bot.add_view(EventApprovalView())`; reload pending/rejected; optional startup log line |

## Edge cases

- **Approve after restart:** view re-registered globally → button still resolves payload from reloaded file.
- **Payload missing on click** (file loss / double click): ephemeral "already handled / expired"; no crash.
- **Event deleted on backend while pending:** harmless — we announce from the stored snapshot on approve;
  admin can simply reject instead.
- **Mods channel unreachable** when posting preview: caught and logged with `⚠️` like other loop failures;
  event is NOT marked seen so it retries next poll (avoids silently dropping an event).
- **Organizer event that later loses its org_name** in a subsequent poll: already seen, so no effect.

## Verification plan

1. `python -m py_compile bot.py` must pass.
2. Reason through the exact poll cycle: organizer event → preview only (no public post); AFC event → public
   post unchanged.
3. Simulate Approve → public embed appears with correct channel + ping; preview shows approver; pending file
   emptied.
4. Simulate Reject → no public post; rejected id recorded; later status flip on that event posts nothing.
5. Restart with a pending approval → button still works (persistent view + reloaded payload).
6. Adversarial review pass (multi-agent) focused on: restart/persistence correctness, status-leak paths,
   race conditions, authorization bypass.
7. Update `CHANGELOG.md` is **not** applicable to this repo (that hard rule targets the scrapling-clone
   project). This repo's required post-change step: `py_compile` + no Claude co-author trailer.
