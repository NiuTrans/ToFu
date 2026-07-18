# RENDER_CONTRACT Phase 4 — W2–W5 landing plan (finish the rev-CAS domain)

> Status: **PLAN + tests-first skeleton only. No writer switched yet.** W1
> (`_sync.py` terminal sync) and W6 (`commit.py` translate) already CAS on
> `rev` (commit `2bd6702`). This doc closes the remaining `conversations.messages`
> CAS writers so the whole domain is on one monotonic token and the last
> same-ms clobber window shuts.

## 0. Why this must land (not stop at W1+W6)

After W1+W6, the system is in a **hybrid-lock middle state**: terminal-sync +
translate CAS on `rev`, but the partial checkpoint, the settle-reconcile, the
queued-user mirror, the swarm snapshot, and the auto-translate stale-clear still
CAS on `updated_at`. The residual same-ms clobber the owner flagged —
"terminal-writes-first same-ms → queued mirror overwrites" — lives on
`append_pending_user_msg`, an `updated_at` writer. It does not close until the
queued-mirror writer is on `rev` too. W2–W5 finish the domain.

## 1. Complete writer inventory (the full CAS `messages`-writer set)

Every row is a read-modify-write `UPDATE … SET messages=? … WHERE … updated_at=?`.
`rev` is a WHERE token ONLY — the `conversations_rev_bump_trg` trigger is the sole
bumper; SET keeps stamping `updated_at` for freshness/ordering.

### 1a. CAN SWITCH NOW (in HEAD `2bd6702`, no sibling-WIP overlap)

| ID | File · function | SELECT (add rev) | baseline var | UPDATE WHERE | retry loop | notes |
|----|-----------------|------------------|--------------|--------------|-----------|-------|
| **W2** | `lib/chat/persistence.py` · `append_pending_user_msg` | L303 `SELECT messages, updated_at` → add `rev` | `cur_updated_at` → add `cur_rev` | L308-310 `updated_at=?` → `rev=?` | ✅ L302 `range(_MAX_CAS=4)` re-read at top | the queued-mirror writer; **this is the writer that closes the owner's residual same-ms window** |
| **W3** | `lib/message_queue.py` · `dispatch_next_queued` helper (~L690) | L705 `SELECT messages, updated_at` → add `rev` | `cur_updated_at` → add `cur_rev` | L722-724 `updated_at=?` → `rev=?` | ✅ L704 loop | ⚠️ the post-exhaustion fallback L751-754 is **deliberately non-CAS** (unconditional, "never drop the queued turn") — LEAVE AS-IS |
| **W4** | `lib/tasks_pkg/auto_translate/_assistant.py` · `_maybe_auto_translate_assistant` | L184 `SELECT updated_at` → `SELECT updated_at, rev` | `_ua_row[0]` → capture rev | L192 `updated_at=?` → `rev=?` | ❌ single-shot best-effort | a `rev`-miss just skips the stale-translatedContent clear (still best-effort; acceptable, no re-read to add) |
| **W-settle** | `lib/tasks_pkg/manager/_sync.py` · `_reconcile_orphan_placeholder_on_settle` | L302 `SELECT messages, updated_at, settings` → add `rev` | `_row_updated_at` → add `_row_rev` | L346 & L353 (both settings branches) `updated_at=?` → `rev=?` | ❌ single-shot | in HEAD; sibling-WIP-free |
| **W-partial** | `lib/tasks_pkg/manager/_sync.py` · `_sync_partial_to_conversation` | L1310 `SELECT messages, updated_at` → add `rev` | `cur_updated_at` | L1423 `updated_at=?` → `rev=?` | ✅ `range(MAX_CAS=3)` re-read at top → must add `rev` to the re-read + refill baseline | in HEAD; sibling-WIP-free |

### 1b. BLOCKED on sibling WIP (do NOT touch until it lands in HEAD)

| ID | File · function | why blocked |
|----|-----------------|-------------|
| **W-abortstamp** | `lib/tasks_pkg/manager/_sync.py` · `_stamp_aborted_fragment_finish_reason` (UPDATE ~L440-444) | HEAD-count=0 — this whole function is **uncommitted sibling WIP** in the working tree (abort-fragment work). Switching its token means editing code that isn't in HEAD; it must ride the sibling's own commit. **Held.** |

### 1c. API-CONTRACT change — separate, careful step (NOT a simple SELECT/UPDATE swap)

| ID | File · methods | why separate |
|----|----------------|--------------|
| **W5a/W5b** | `lib/tasks_pkg/persistence_store.py` · `cas_update_conversation_messages` (L91), `cas_sync_conversation_with_search` (L104) | These are stateless CAS primitives: the baseline arrives as the **`expected_updated_at` parameter**, read by the caller (`load_conversation_messages` L52-73, SELECTs `messages, updated_at`). Migrating them changes a **public method signature** (`expected_updated_at` → `expected_rev`) and every caller. Larger blast radius; do it as its own reviewed step AFTER 1a, with its own caller audit. **Deferred within this plan.** |

### 1d. Explicitly OUT OF SCOPE (not CAS `updated_at` writers — do not migrate as CAS)

- `lib/message_queue.py` L752-754 — non-CAS exhaustion fallback (must stay unconditional).
- `lib/tasks_pkg/persistence_store.py` `save_conversation_messages` (L82), `sync_conversation_with_search` (L288) — non-CAS unconditional.
- `lib/chat/persistence.py` `persist_conv_messages` (`upsert`, C1) + `dual_write_conv` (C2) — creator/mirror, CAS-exempt (proven by T5b).
- `lib/swarm/snapshot.py` · `persist_snapshot_to_conversation` (**W5** in the raw map): swarm-artifact snapshot writer. In scope for the domain, sibling-WIP-free, `range(_MAX_CAS=6)` loop. **Include in batch 1a** (below) — kept off the headline table only because it's a lower-traffic path; same 3-site swap (SELECT+baseline+UPDATE+re-read).

## 2. The invariant every switch preserves

1. `rev` appears **only** in `WHERE`, never in `SET` (trigger is sole bumper).
   Guarded by `test_no_writer_stamps_rev_in_set_clause` — extend it to scan
   these files too.
2. A writer with a retry loop must add `rev` to BOTH the initial SELECT and the
   in-loop re-read, and refill the baseline var from the fresh `rev` each miss.
3. A best-effort single-shot writer (W4) simply reads `rev` and guards on it; a
   miss is the same "skip" it already tolerates on `updated_at`.

## 3. Landing order (each step independently committable + reversible + tested)

1. **Batch 1a** (this plan's deliverable): W2, W3, W4, W-settle, W-partial, W5(snapshot).
   All in HEAD, sibling-WIP-free, mechanical 3-site swaps. One commit; run T-queue
   (below) + the W1+W6 suite + `test_no_writer_stamps_rev_in_set_clause` extended
   to the new files.
2. **Batch 1c** (separate follow-up): W5a/W5b parameter rename + caller audit.
3. **W-abortstamp**: switch when the sibling's abort-fragment WIP lands in HEAD
   (coordinate; do not touch their uncommitted code).

## 4. Tests-first

- **T-queue** (`test_queue_mirror_same_ms_no_clobber`): reproduce the residual
  window the owner named — a terminal-sync write lands, then `append_pending_user_msg`
  (queued mirror) fires in the SAME millisecond (frozen clock). On `updated_at`
  the mirror's CAS passes and overwrites the terminal answer; on `rev` the trigger
  bumped rev so the mirror MISSES → re-reads → appends onto the fresh tail →
  BOTH survive. Tests-first RED on HEAD (updated_at), GREEN after W2 switches.
- Extend **`test_no_writer_stamps_rev_in_set_clause`** to also scan
  `chat/persistence.py`, `message_queue.py`, `auto_translate/_assistant.py`,
  `swarm/snapshot.py` — assert none writes `rev` in a SET clause.
- Re-run the W1+W6 suite (must stay green) + sibling regressions
  `test_terminal_cas_retry.py`, `test_auto_translate_safety_net.py`.
- Both backends: SQLite (pytest) + live PG (direct-call harness), per the
  W1+W6 precedent.
