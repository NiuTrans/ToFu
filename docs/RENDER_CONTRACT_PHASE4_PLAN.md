# RENDER_CONTRACT Phase 4 — landing plan (CAS on `rev`)

> **Status: §2.2 (regraft merge) LANDED in the working tree (owner-approved,
> UNCOMMITTED — pending owner review of the diff before step 2). Steps 2–4
> (token swap) still awaiting sign-off.**
> §2.2 delivered: `_merge_terminal_fields` + `_merge_segments_preserving_translations`
> in `_sync.py` (owned-whitelist merge, nested segments merge), and
> `tests/test_rev_cas_migration.py` (token proof GREEN + its updated_at NEUTER;
> regraft preservation incl. a per-segment `translatedText` variant GREEN; a
> whole-dict-replace NEUTER proving the merge is load-bearing). W1+W6 token swap
> is step 2 and is NOT done — the regraft still CASes on `updated_at`.
>
> Companion: [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md) §4 (persistence contract),
> Invariant 4, migration table row **Phase 4**.

---

## 0. The one thing that makes this NOT a mechanical `s/updated_at/rev/`

Two facts, established by reading the code, change the shape of the work:

1. **The terminal regraft is a REPLACE, not a merge.** `_sync.py:985-991`, on a
   CAS miss, does `_fresh_messages[-1] = last_msg` — it overwrites the fresh
   tail with the backend's assembled assistant dict, which carries **no**
   `translatedContent` / `_showingTranslation` / `segments[].translatedText` /
   `originalContent`. Today translate-commit and terminal-sync race on two
   *different-ish* `updated_at` clocks, so the regraft rarely fires against a
   just-translated tail. **Once both CAS on `rev`, they contend on ONE monotonic
   token → CAS-miss becomes frequent → the regraft fires often → it silently
   drops concurrent translations.** This is exactly the "把偶发同步丢失变成高频同步丢失"
   the owner warned about. `test_terminal_regraft_preserves_concurrent_translation`
   pins it (RED on HEAD).

2. **Two writers are already `rev`-gated; one class must NEVER be gated.**
   `translate/segment_backfill.py` and the frontend PUT (`_save_conv_blocking`)
   already CAS on `rev`. `persist_conv_messages` (send/create) uses `upsert()`
   with **no CAS** and MUST stay that way — it is the turn CREATOR, expected to
   have no concurrent writer; adding CAS there manufactures false 409s (the
   owner's point 2).

So Phase 4 = **(a)** switch the concurrent-writer CAS token to `rev`, **(b)** fix
the regraft to a field-level merge so higher miss frequency is safe, **(c)**
re-classify translate as a normal versioned write, **(d)** draw the CAS-domain
boundary explicitly and dispose of the dual-write (L5).

---

## 1. Writer inventory (verified against HEAD)

| # | Writer | Location | CAS today | Phase-4 target | Notes |
|---|---|---|---|---|---|
| W1 | terminal sync | `manager/_sync.py:942-955` (loop), regraft `985-991` | `updated_at`, 3× | **`rev`** + **merge regraft** | The load-bearing one (§2). |
| W2 | partial checkpoint | `manager/_sync.py:1434-1444` | `updated_at`, 3× | **`rev`** | Same loop shape; no regraft (it re-reads + re-applies fields). |
| W3 | settle orphan reconcile | `manager/_sync.py:255-267` | `updated_at`, 1-shot | **`rev`**, 1-shot | On miss it already returns "concurrent won (safe)" — keep, just swap token. |
| W4 | aborted-fragment stamp | `manager/_sync.py:354-357` | `updated_at`, 1-shot | **`rev`**, 1-shot | Idempotent stamp; keep safe-skip on miss. |
| W5 | queued-user append | `chat/persistence.py:append_pending_user_msg` | `updated_at`, 4× | **`rev`** | Order/slot gates unchanged; only the CAS token + read-back move. |
| W6 | translate commit | `translate/commit.py:244-248` | `updated_at`, 5× | **`rev`** | Becomes a normal versioned write (§3). |
| W7 | segment backfill | `translate/segment_backfill.py:354` | **`rev`** | `rev` | Already done — the reference shape. |
| W8 | frontend PUT | `routes/conversations.py:_save_conv_blocking` | **`rev`** (`baseRev`) | `rev` | Already done — fail-open when `baseRev` absent. |
| C1 | send/create | `chat/persistence.py:persist_conv_messages` | **none** (`upsert`) | **none — OUT of CAS domain** | Turn creator; CAS here = false 409. |
| C2 | dual-write mirror | `database/messages_rows.py:dual_write_conv` | uncoordinated | **declare OUT of domain** (§4) | L5. Flag-gated best-effort mirror. |

The migration is W1–W6 (six writers) + the regraft fix + the dual-write
disposition. W7/W8 are the proof the target shape already works in-tree.

---

## 2. W1 terminal sync — the exact edit + why the merge is the crux

### 2.1 Token swap (the easy half)

The read at `_sync.py:493-495` and the CAS at `942-955` change from
`updated_at` to `rev`:

```
# read
SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1
_row_rev = row['rev']
# CAS (both settings/no-settings branches)
UPDATE conversations SET messages=?, updated_at=?, msg_count=?, [settings=?,] search_text=?
  WHERE id=? AND user_id=1 AND rev=?          # ← was updated_at=?
  (…, conv_id, _row_rev)
```

`updated_at` is still WRITTEN (sidebar ordering) — it just stops being the CAS
predicate. On a miss, re-read `rev` from the fresh row and retry with it.

### 2.2 The regraft MUST become a field-level merge (the crux)

Replace the whole-dict overwrite at `_sync.py:985-991`:

```python
# ── TODAY (drops backend-non-owned fields on the fresh tail) ──
if _fresh_tail.get('role') == 'assistant':
    _fresh_messages[-1] = last_msg          # ← REPLACE: clobbers translatedContent etc.
else:
    _fresh_messages.append(last_msg)
```

with a merge that grafts ONLY the fields the terminal sync authoritatively owns
onto the fresh tail, preserving everything the backend never produces:

```python
# ── PHASE 4 (merge: our terminal fields win; backend-non-owned fields survive) ──
if _fresh_tail.get('role') == 'assistant':
    _merge_terminal_fields(_fresh_tail, last_msg)   # in-place graft, keep fresh extras
else:
    _fresh_messages.append(last_msg)
```

`_merge_terminal_fields(dst_fresh, src_terminal)` copies the BACKEND-OWNED set
from `src_terminal` onto `dst_fresh`:

- owned (overwrite): `content`, `thinking`, `finishReason`, `usage`,
  `toolSummary`, `toolRounds`, `model`, `provider_id`, `apiRounds`,
  `modifiedFiles`, `modifiedFileList`, `cost`, `_gitSha`, the inbox/steer
  sidecars, `_memoryPrefetch`, `_preferencesApplied/Learned`, segments (the
  backend's settled segments), `_msgId`/timestamp identity.
- **preserved (never touched if already on the fresh tail):**
  `translatedContent`, `_showingTranslation`, `_translateDone`,
  `_translateModel`, `originalContent`, and `segments[i].translatedText`
  (per-segment narration translations stamped by W6/W7).

The preserved set is precisely "what auto-translate writes and the terminal sync
does not". This is the single-source-of-truth boundary between W1 and W6 written
as a merge policy. The RED test flips GREEN when this lands; a NEUTER (revert to
whole-dict replace) must re-drop the translation.

> Placement: define `_merge_terminal_fields` next to `_new_assistant_slot` in
> `manager/_sync.py` (or `manager/_events.py` beside `find_message_by_id`), so
> it is the ONE place that encodes "backend-owned vs translate-owned". Do not
> inline the field list at the regraft site — a future field added to one side
> must update one policy, not a scattered literal (the same class of hidden
> obligation RENDER_CONTRACT set out to kill).

---

## 3. W6 translate — from "旁路 mutation" to "normal versioned write"

Today `_commit_translation_inner` (`translate/commit.py`) reads `updated_at`,
CASes on it, and calls `notify_conv_changed(rev=<read-back>)`. Two changes:

1. **CAS on `rev`.** Read `messages, rev`; CAS `WHERE … AND rev=?`; on miss
   re-read `rev` + retry (the 5× loop already exists — only the token moves).
   Its per-conv `threading.Lock` stays (serializes same-process translate
   threads; `rev` covers cross-process/other-path writers).
2. **It already bumps `rev`** (any messages change trips the trigger) **and
   already pushes the post-write `rev`** on `conv_changed`. So "translate becomes
   a versioned write" is mostly *recognising* it already is one, once its CAS
   token matches everyone else's. The client already refetches by the standard
   rev-gate on that push (W8 path). No second CAS regime remains: after W1+W6
   both CAS on `rev`, the two regimes RENDER_CONTRACT §4.2 warned about collapse
   into one.

The `_commit_translation_inner` id→idx→content resolution order is unchanged (it
targets the message by stable `_msgId`, which is correct and orthogonal to the
CAS token).

---

## 4. CAS-domain boundary (owner point 2) + dual-write (L5)

**In the `rev`-CAS domain** (concurrent post-turn writers of an EXISTING row):
W1–W8. Rule: read `(messages, rev)`, compute, `UPDATE … WHERE rev=?`, on miss
re-read + re-graft/retry.

**Explicitly OUT of the domain:**

- **C1 `persist_conv_messages` (send/create).** The turn creator. It uses
  `upsert()` and must NOT CAS — a new turn has no concurrent writer, and a CAS
  there would 409 the very write that establishes the assistant slot. It still
  bumps `rev` (via the trigger) and returns the read-back `rev` for the
  cross-device notify — it just never *guards* on `rev`. Documented as
  domain-exempt, not "forgotten".
- **C2 `dual_write_conv` (L5).** A flag-gated (`TOFU_MESSAGES_ROWS`),
  best-effort mirror of the JSONB array into `conversation_messages` rows. It is
  **not** an authoritative store and must not join the `rev` CAS (it would add a
  second gate on the hot write path for a mirror that already swallows all
  errors). **Disposition: declare it out-of-domain** — it runs AFTER the
  authoritative JSONB write inside the same `persist_conv_messages` call, mirrors
  whatever landed, and its failure never blocks the real write. If it is ever
  promoted to authoritative (Phase 5), it re-enters the domain then. Recorded so
  L5 is *decided*, not dangling.

---

## 5. Migration tests (this round's deliverable)

`tests/test_rev_cas_migration.py` — standalone runner + pytest, real DB,
mirrors `test_terminal_cas_retry.py`:

| Test | Asserts | HEAD |
|---|---|---|
| `test_rev_distinguishes_same_ms_writers` | Two writers read the same row; B commits; A's stale-`rev` CAS MISSES → B preserved. **NEUTER**: `updated_at` CAS with a forced same-ms stamp lets A CLOBBER B (the L3 loss). | **GREEN** — proves the token choice is load-bearing at the DB level. |
| `test_terminal_regraft_preserves_concurrent_translation` | A translation lands on the tail in the terminal read→write window (forces regraft); the final answer AND `translatedContent` must both survive. | **RED (tests-first)** — HEAD's whole-dict regraft drops the translation. This IS the spec §2.2 must satisfy. |

Additional tests to add WHEN §2–3 land (so the landing is fully guarded):

- **T3 — high-frequency contention**: fire W1 + W6 against one conv concurrently
  N× and assert every final answer AND every translation persisted (the
  "重试路径必须零丢失" acceptance under the new, higher miss rate).
- **T4 — merge NEUTER**: revert `_merge_terminal_fields` to a whole-dict replace
  → T2 must re-fail (proves the merge is what preserves the field).
- **T5 — creator stays CAS-free**: a `persist_conv_messages` send onto a rev>0
  row must NOT 409 (proves C1 is out of domain).
- **T6 — partial/settle/stamp/queue token swap**: each of W2–W5 survives a
  same-ms concurrent bump the way W1 now does (one focused case each).

---

## 6. Landing order (each independently shippable + revertible, test-gated)

1. **§2.2 regraft merge FIRST** (behind no flag — it is strictly safer even on
   the current `updated_at` CAS): flips T2 GREEN, add T4 NEUTER. This removes the
   data-loss hazard BEFORE raising miss frequency. Low blast radius.
2. **§2.1 W1 + §3 W6 token swap** together (they are the pair that starts
   contending): add T3 contention + T5 creator-exempt.
3. **W2–W5 token swap**: add T6.
4. **§4 dual-write disposition**: doc + a one-line comment at the call site; no
   behaviour change.

Rationale: step 1 is a pure correctness fix that pays off immediately and de-risks
everything after it; steps 2-3 are the token convergence; step 4 is bookkeeping.

---

## 7. What this does NOT touch

- **Phase 3** (unified `apply()` reducer, `roundNum` unification,
  `round_start/end`) — separate epic, owner-gated, after Phase 4.
- **The send-time canonical serialization** epic (`pt_b62e8192`, sibling
  `mrojppybal8tcc`) — that normalizes the wire bytes sent to the MODEL; Phase 4
  is the CAS token for writes to the DB. Disjoint surfaces (no shared file), but
  both touch "message consistency" — I will confirm the boundary with that
  sibling before editing any shared writer.
