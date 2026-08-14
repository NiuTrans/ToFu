# Turn-Settlement Verdict — a single backend-authoritative fact per assistant turn

> **Status:** LANDED (P1 + P1b + A/B + C1 + C2 + C3). The three consumers
> (interrupt bubble, Continue button, lossless resume) all read the single
> verdict. Backend-authoritative via a behavior-locked canonical JS port.
> **Epic:** `pt_a4484f3ad3134ea8` (COMPLETE).
> **Companion to (not a duplicate of):** `pt_e1c4693341b24730` (`pt_conv_state_ssot`).
> That epic builds the authoritative channel for *which convs are busy*; this one
> defines the authoritative content of *how one turn ended and how it can be
> resumed*.
>
> **Delivery model — canonical port, not persist+propagate.** The backend
> `compute_turn_settlement` is the single definition; the frontend port
> `computeTurnSettlement` (`static/js/core/turn_settlement.js`) is
> behavior-locked to it by `tests/test_frontend_turn_settlement_equivalence.py`
> (both driven over one corpus, deep-equal asserted). Both sides RECOMPUTE the
> verdict from the persisted message fields — the exact ghost-tail precedent
> (`classify_ghost_tail` ↔ `_classifyGhostTailJS`) and conv_state_reducer. See
> §5 for why persist+propagate (the original P2) was deliberately rejected.

---

## 1. The problem (why these three things must be done together)

Three UI behaviours all read the **same loosely-controlled `finishReason`
string**, each re-deriving its own conclusion:

| Consumer | File | What it infers today |
|---|---|---|
| Interrupt bubble label | `static/js/ui/finish_info.js:790` | maps `finishReason` → a label/tooltip |
| Continue-button gate | `static/js/ui/chat_render.js:1586` | "not in the clean list → show Continue" |
| Resume-mode decision | `lib/tasks_pkg/segments/_types.py:57` + `lib/chat/turn_builder.py:443` | "in the resumable list → prefill, else checkpoint-scan, else regenerate" |

`finishReason` itself is stamped by **≈5 different code paths** (orchestrator
finalize, endpoint runner, abort handlers, the frontend's optimistic Stop,
the GET/startup reconcile, the poll-fallback). There is no single authority
and no normalization. The consequences:

1. **The Continue button over-promises, then silently degrades.** It shows
   "Continue generating from where it left off" for `aborted` / `error` /
   `abnormal_stop` / a *missing* `finishReason` alike — but it never checks
   whether a recoverable checkpoint actually exists. When it doesn't
   (`scan_continue_checkpoint` → `None` and prefill declined), the click falls
   through to `data.fallback === 'regenerate'` (`main_regen_continue.js:344`)
   and **regenerates the whole turn** — a different operation than the button
   advertised.

2. **Resume is not honestly lossless, and one common case loses everything.**
   A **manual Stop** stamps `finishReason='aborted'`, which is **not** in
   `RESUMABLE_FINISH_REASONS`. On a no-tools turn with a prefill-capable model,
   Stop → Continue therefore regenerates from scratch and **discards the
   partial prose** — the single most common "resume my interrupted answer"
   action loses the answer. The user cannot tell in advance whether Continue
   will be lossless, lossy (checkpoint), or a full regeneration.

3. **Cross-device, the bubble shows a stale truncated state** (the ssot epic's
   symptom driver) because the settled fact is re-inferred per client instead
   of propagated as one frame.

## 2. The verdict

Compute **once**, at settle time (and identically on a cold reopen from the
persisted message fields), a typed fact persisted on the assistant message as
`msg['_settlement']` and carried in the `done` / poll payload:

```
_settlement = {
  # WHAT happened — one normalized outcome (the closed enum):
  'outcome': 'completed' | 'interrupted' | 'truncated' | 'failed',

  # the raw provider/transport reason, verbatim, for debugging + compat:
  'finishReason': '<raw>',

  # WHY — a single cause dimension (replaces the scattered interruptedReason
  # + reason-string sniffing):
  'cause': 'manual' | 'killed' | 'restart' | 'offline' | 'gateway'
         | 'max_tokens' | 'tool_cap' | 'safety_cap'
         | 'content_filter' | 'error' | null,

  # CAN it be resumed, and HOW — decided HERE, once, not per consumer:
  'resume': {
    'mode': 'prefill' | 'checkpoint' | 'regenerate' | 'none',
    'lossless': bool,      # prefill=True; checkpoint=False (drops the prose tail)
    'keptRounds': int,     # checkpoint mode
    'prefillChars': int,   # prefill mode
    'reason': str,         # why this mode (esp. why regenerate / none)
  },
}
```

### 2.1 Mapping (raw `finishReason` → outcome / cause)

| raw finishReason | outcome | cause | resumable via |
|---|---|---|---|
| `stop` / `end_turn` / `stop_sequence` | `completed` | — | — (nothing to resume) |
| `length` / `max_tokens` | `truncated` | `max_tokens` | prefill |
| `tool_rounds_exhausted` *(legacy persisted turns only)* | `truncated` | `tool_cap` | checkpoint |
| `incomplete` (endpoint/autopilot cap) | `truncated` | `safety_cap` | checkpoint |
| `interrupted` | `interrupted` | `killed`/`restart` (from `interruptedReason`) | prefill |
| `server_offline` | `interrupted` | `offline` | prefill |
| `premature_close` | `interrupted` | `gateway` | prefill |
| `aborted` (manual Stop) | `interrupted` | `manual` | **prefill** *(the gap fix)* |
| `error` | `failed` | `error` | — |
| `content_filter` | `failed` | `content_filter` | — |
| `abnormal_stop` | `failed` | `error` | — |
| *missing / unknown* | `interrupted` | `null` | best-effort (prefill/checkpoint) |

The *missing / unknown* row deliberately keeps the recovery path open — it
mirrors today's "a legacy turn with no finishReason still shows Continue".

### 2.2 `resume.mode` precedence (P5 flip, owner-approved `d4811ff1`)

1. `outcome == 'completed'` → `none`
2. empty turn (no content / thinking / real tool round) → `regenerate` (`'empty_turn'`)
3. prefill-capable model + resumable finishReason + terminal deliverable text →
   **`prefill`** (`lossless=True`). The continue route ALREADY ships this
   lossless case-2 wire for a capable tools turn (replay the tool batch +
   prefill the tail), so the verdict now reports it honestly. `keptRounds` is
   still surfaced (the route replays those rounds alongside the prefill).
4. completed tool rounds exist (`toolCallId`s) but prefill is unavailable
   (Claude / no resumable tail / a non-resumable finishReason like `error`) →
   **`checkpoint`** (`lossless=False`) — replay the rounds, regenerate the tail.
5. else → `regenerate` (`'no_checkpoint_no_prefill'`)

**prefill is now preferred over checkpoint** for a prefill-capable model with a
resumable terminal tail. The parity validation
(`tests/test_continue_prefill_over_checkpoint_parity.py`) proved the
prefill+toolHistory combination byte-correct, and the route probe
(`tests/test_continue_tools_turn_route_behavior.py`) established that the
continue route was ALREADY shipping the lossless case-2 wire — the flip makes
the verdict + the route response (`resumeMode='prefill'`, `priorContent=''`)
honest about it. Checkpoint remains the honest fallback.

## 3. The three consumers stop re-inferring

- **Bubble label** (`finish_info.js`) reads the verdict's `outcome` + `cause`
  via `finishLabelForSettlement` → one label map, byte-identical output. The
  `interruptedReason`-sniffing branch is DEMOTED to a defensive fallback used
  only when `turn_settlement.js` (bundle-only) is not loaded — in the browser
  the verdict drives it.
- **Continue button** (`chat_render.js`) reads `resume.mode` via
  `continueButtonForSettlement`: `prefill` → "Continue (lossless)";
  `checkpoint` → "Continue (from round N)"; `regenerate` → relabel honestly to
  "Regenerate"; `none` → hidden. The button can no longer over-promise. (The
  button decides the LABEL only; the actual resume/regenerate decision stays
  server-authoritative, see next.)
- **Continue executor** (`main_regen_continue.js`) was ALREADY
  backend-authoritative (defers to the server's checkpoint scan and handles the
  `fallback:regenerate` case) — so it needed no change. The dishonesty lived in
  the button LABEL, which is what C2 fixed. The `/api/chat/continue` route and
  the client verdict compute the SAME resume point (both via
  `scan_continue_checkpoint` / `resume_prefill_from_segments`), so client and
  server cannot disagree.

## 4. Robustness properties

- **Deterministic & pure** — `compute_turn_settlement(msg, *, model, segments)`
  is a pure function over the persisted message fields; a cold reopen recomputes
  the identical verdict for legacy messages that lack `_settlement`.
- **Cache-prefix neutral** — the verdict is a NEW key on the message dict; it
  is not part of the wire fingerprint, so stamping it never busts the prompt
  cache.
- **Fail-closed** — any uncertainty (unknown model, missing segments) degrades
  to the *current* behaviour (checkpoint-scan / regenerate), never to a
  riskier resume.

## 5. Phasing — LANDED (one commit per phase, failing-first + NEUTER + collect gate)

- **P1** *(landed, `4e75c586`)* — `lib/conversations/turn_settlement.py`: the pure
  verdict SSOT + 32 tests. Zero production-behaviour change.
- **P1b** *(landed, `4e75c586`)* — extend `RESUMABLE_FINISH_REASONS` with `'aborted'`
  (manual Stop) so a stopped turn with content on a capable model resumes via
  lossless prefill instead of full regeneration. The single concrete lossless
  gap fix.
- **A + B** *(landed, `38d48669`)* — prove the LIVE chain (manual-Stop →
  `persist_task_result` → `/api/chat/continue` → prefill, not regenerate) with a
  real-DB integration test; and plug the segments-missing hole
  (`resume_prefill_from_segments` falls back to `msg['content']` when no terminal
  deliverable segment exists — the `deliverable_text` precedent).
- **C1** *(landed, `49795315`)* — canonical JS port `static/js/core/turn_settlement.js`
  + `_BUNDLE_FILES` registration + the backend↔frontend equivalence lock.
- **C2** *(landed, `814abd3c`)* — the Continue button reads `resume.mode` with an
  honest per-mode label (prefill→Continue-lossless / checkpoint→Continue-from-N /
  regenerate→**Regenerate**, no longer masquerading as Continue).
- **C3** *(landed, `4d2d14d3`)* — the interrupt bubble reads the verdict via
  `finishLabelForSettlement` (byte-identical output), plus the `CAUSE_UNKNOWN`
  refinement (faithful 3-way killed/restart/unknown) and a robust legacy
  kind-derivation fallback so the bubble stays correct when `turn_settlement.js`
  (bundle-only) is absent.
- **P2 — deliberately REJECTED (not unfinished).** The original plan was to
  compute the verdict at settle and PERSIST `msg['_settlement']` + propagate it in
  the `done`/poll payload ("compute once server-side"). That was dropped in favour
  of the canonical-port / recompute model actually shipped, for three reasons:
  (1) **drift** — a persisted `_settlement` freezes a stale snapshot the moment
  the verdict logic is refined (this is exactly what happened when `CAUSE_UNKNOWN`
  was added after the fact; a recompute always reflects the current logic). This
  is why the project's ghost-tail classifier *recomputes on serve* and never
  persists a classification. (2) **precedent** — ghost-tail and conv_state_reducer
  both share backend logic with the frontend via a behavior-locked canonical port,
  not persistence. (3) **risk** — persisting touches `_sync_result_to_conversation`
  (freshness-guard + CAS + active sibling work) and the `done` event (wire-parity
  gates across many suites) for zero behavioural gain over the equivalence-locked
  port. The verdict is still backend-authoritative: the Python definition is the
  single source, and the frontend cannot diverge from it.
- **P4** *(landed, folded into C2/C3)* — the three independent inferences
  (`_FINISH_CLEAN` gate, the finishReason→label sniffing, the `interruptedReason`
  branch) are replaced by the verdict in the production path. A compact legacy
  kind-derivation is RETAINED in `finish_info.js` purely as a defensive fallback
  for the bundle-absent case (dev-mode script-tag fallback / JSDOM harness that
  loads `finish_info.js` without `turn_settlement.js`) — removing it would regress
  the bubble in those contexts, so it is an intentional safety net, not the
  re-inference the epic set out to kill.
- **P5** *(landed, `d4811ff1`, epic `pt_c11c3a9272274848`)* — prefer prefill
  over checkpoint for capable models on tool turns, owner-approved via
  question-block ("A — FLIP"). Parity validation (`c4c87dfa`) proved the
  prefill+toolHistory combination byte-correct; the route probe
  (`tests/test_continue_tools_turn_route_behavior.py`) then established the
  continue route was ALREADY shipping the lossless case-2 wire (the tail is NOT
  dropped on the wire) — the actual dishonesty was in the REPORTING (verdict
  said checkpoint/lossy, response said `resumeMode='checkpoint'` +
  `priorContent=<tail>`). The flip makes the verdict report prefill (lossless)
  and the route report `resumeMode='prefill'` + `priorContent=''`, with
  checkpoint as the honest fallback (Claude / no resumable tail / non-resumable
  finishReason like `error`).
