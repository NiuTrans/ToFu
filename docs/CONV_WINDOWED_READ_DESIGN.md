# Windowed conversation read — root-cause fix for slow first-open of long conversations

Status: DESIGN (awaiting owner sign-off before landing steps 3–5)
Owner epic: conversation-messages read cutover + tail-windowed load
Related landed work: `tofu/integration @ db55328` (no-refresh history_rewrite
push + write-free GET; removed the read-path FUSE fsync — 94% of the old read
path, measured in `debug/bench_get_conv_readpath.py`).

## 1. Why this exists (the measured residual)

The landed write-free GET removed the inline reconcile `UPDATE+commit` fsync
from the read path. The benchmark then showed the residual cost of a cold
first-open is dominated by the **single-blob full-history load**:

| turns | blob | NEW read-only GET | dominant residual |
|------:|-----:|------------------:|-------------------|
|   40  | 135 KiB |  0.39 ms | SELECT (blob fetch) |
|  400  | 1.35 MiB | 3.17 ms | SELECT |
| 1200  | 4.05 MiB | 9.54 ms | reconcile compute + deserialize |

All three residual phases — `SELECT` (detoast the whole `messages` blob),
`json.loads`, and reconcile scanning every message — grow **linearly with
history length**, because the schema stores the entire transcript as one JSON
blob in `conversations.messages` and the GET loads + parses all of it.

Root cause answer to the original question ("is it the database design?"):
**yes — the single-blob schema is the root of slow first-open for long
conversations.** The fix is to serve reads from the already-built normalized
`conversation_messages` row store, windowed to the tail.

## 2. Current state (code-verified 2026-07-12)

Infrastructure is migrator-first and already solid; only the READ side is
missing.

READY:
- Table `CONVERSATION_MESSAGES` (`lib/database/_core_schema.py:384`): PK
  `(conv_id, seq)`, `msg_id`, hoisted `role/content/content_json/thinking/
  translated_content`, full-fidelity `meta` JSONB, `created_at/updated_at`.
- Index `idx_conv_msgs_conv ON conversation_messages(conv_id, seq)`
  (`_schema_sqlite.py:223` + PG equivalent) — the key for cheap tail windows.
- Dual-write wired at BOTH write paths: `routes/conversations.py:1271`
  (save_conv) and `lib/chat/persistence.py:193` (task persist), via
  `dual_write_conv` (flag-gated, best-effort, never raises).
- Idempotent `backfill_conv`; lossless `message_to_row`/`row_to_message`;
  `test_messages_rows.py` 8/8 green.
- Read cutover flag `rows_read_enabled()` = `TOFU_MESSAGES_ROWS` AND
  `TOFU_MESSAGES_ROWS_READ` (both default OFF, decoupled).
- Verification gate `verify_conv_parity` / `verify_search_text_parity`
  (byte-identical `build_search_text` before any read flip).

MISSING (this epic):
- A tail-window SELECT (`messages_rows.py` only has full `rows_to_messages`).
- Any caller of `rows_read_enabled()` — reads are still 100% single-blob via
  `_conv_row_to_dict`.
- Frontend incremental (scroll-up) pagination — `loadConversationMessages`
  loads the whole array once.

## 3. Contract — windowed GET

`GET /api/conversations/<id>?window=<N>&before_seq=<S>`

- No params → current full-array behavior (backward compatible; old clients
  and the prefetch path are unaffected).
- `window=N` → return only the **tail N** messages plus pagination meta:
  `{ totalCount, firstLoadedSeq, hasMore }`.
- `before_seq=S` → page upward: messages in seq range `[max(0, S-N), S)`.
- Default N configurable via `TOFU_CONV_WINDOW` (e.g. 60); unset/0 = no window.

## 4. Backend — tail-window read (new, no change to the substrate)

New in `lib/database/messages_rows.py` (pure SELECT on `idx_conv_msgs_conv`):

```
load_message_window(db, conv_id, limit=N, before_seq=None)
    -> (msgs, total, first_seq, has_more)
```

- `SELECT meta FROM conversation_messages WHERE conv_id=? [AND seq<?]
   ORDER BY seq DESC LIMIT ?` then reverse to ascending.
- `total` from `conversations.msg_count` (existing column — zero extra scan) or
  `SELECT COUNT(*)`.
- Cost drops from linear-in-history to constant-in-window: only N rows'
  `meta` are detoasted/parsed, not the whole blob.

## 5. GET read-path wiring (gated, fail-open)

- In `get_conv`: when `rows_read_enabled()` AND the request carries `window`,
  serve via `load_message_window`; otherwise take the current single-blob path
  verbatim (byte-identical; single-box default unchanged).
- Any row-read error → fail-open to the single-blob path (same posture as the
  existing reconcile fail-open).

## 6. reconcile within the window (correctness)

- Today `_compute_reconcile` deserializes the FULL array to judge the trailing
  ghost/husk. A ghost tail / superseded-error-husk can only ever be at the
  **tail**, so reconcile only needs the tail window to reach the same verdict —
  eliminating the "deserialize everything just to judge the tail" cost.
- Safety invariant: the reconcile `cache_prefix_count` protection guards the
  **head** prefix; the window is the **tail**. They do not overlap, so
  windowed reconcile cannot delete a prefix message. Husk-collapse is also
  tail-adjacent and covered.
- Invariant to hold: `window >= max tail span reconcile inspects` (currently 2:
  a `[user, ghost-assistant]` or `[error-husk, settled-assistant]` pair). N≥60
  satisfies it with wide margin.

## 7. Frontend — incremental load

- `loadConversationMessages` first fetches `?window=N`; renders the tail N +
  a top "load earlier" sentinel.
- Scrolling to the top triggers `?before_seq=firstLoadedSeq&window=N`, which
  prepends earlier messages while preserving the scroll anchor (no jump).
- IndexedDB cache stays compatible: a cache hit still paints first; the window
  only changes how much the server returns.

## 8. Landing order (each step gated + revertible)

1. `load_message_window` + unit tests (tail / page-up / boundary). Pure add.
2. GET wiring (`rows_read_enabled()`-gated) + windowed reconcile + fail-open
   tests.
3. Frontend scroll-up pagination.
4. Benchmark: extend `bench_get_conv_readpath.py` with a normalized+windowed
   branch; prove on a 1200-turn conv that first-open cost is
   **constant-in-window, not linear-in-history** (measured, not asserted).

## 9. Invariants / boundaries

- Single-box default byte-identical: everything gated OFF; no `window` param =
  current behavior. Matches the scale-out rollout invariant.
- JSONB stays authoritative; rows are a mirror. `verify_conv_parity` must be
  green on real data before flipping `TOFU_MESSAGES_ROWS_READ`.
- Write-set for this epic: `lib/database/messages_rows.py` (+window fn),
  `routes/conversations.py` (GET wiring), `static/js` load path, `debug/bench_*`.
  Claim these paths (project_claim_path) before landing to avoid sibling churn.
```
