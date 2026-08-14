# RENDER_CONTRACT Phase 3.5 — make the DOM-apply layer single-seamed too

> **Status: Phase 3.5 COMPLETE** — §5 steps 1–5 + §7 (streamBufs retirement) + §5-final
> (the allowlist freeze) all LANDED. Steps: 1 design + failing-first skeleton; 2
> `ConvView.apply` + translation_render convergence + census join + boot hard check;
> 3 seam hardening ①collapse/②live-guard/③order-invariant/④dead-CSS + §2.5/2.6/2.8
> convergence; 4 SEAM-2 fold + raw-fallback deletion + boot-check RUNTIME proof;
> 5 zero bare `renderChat(` + §7.4 RED anchor; §7 streamBufs fully retired
> (phase → streamSessions entity, live-only writes); §5-final: the ratchet is
> FROZEN as the floor and the session key contract (`{phase}` only) is guarded
> (streamBufs-v2 door closed). Byte-parity: narration anchor GREEN (step 2),
> reconnect anchor GREEN (§7).
> Companion: [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md) §1 Invariant 1 (`DOM = render(messages, rev)`),
> [`RENDER_CONTRACT_PHASE3_PLAN.md`](RENDER_CONTRACT_PHASE3_PLAN.md) (the message-document reducer).
> This plan closes the gap Phase 3 did NOT close: **Phase 3 unified the message-document
> projection (live/warm/cold/poll → `{content, thinking, toolRounds}`). It did NOT unify
> the DOM-apply layer.** `conv.messages` is the SSOT, but 217 raw DOM writes (census-complete
> as of step 2 — the first-pass 14-file/157 tally missed `streaming_ui.js`, the hottest
> per-frame writer, and `health_stream_timer.js`; see §2.15) still reach `#chatInner`
> without going through ConvView, so the rendered DOM is NOT yet a pure
> function of the message document.
>
> **Measurement note (2026-07-24):** raw-op counts come from the single-pass
> tokenizer in `tests/test_frontend_dom_seam_convergence.py::_scan_raw_dom_ops`
> (regex-stripping undercounts — it swallowed 22 of 23 ops in
> `main_send_pipeline.js` before the tokenizer replaced it).
>
> **Owner sign-off (2026-07-24):** Phase 3 is the long-term plan, not a quick win. This
> commit delivers the design + the per-site reclassification table + the failing-first
> byte-parity test skeleton. Boundary: this write-set (`docs/RENDER_CONTRACT_PHASE3_5_PLAN.md`,
> `tests/test_frontend_dom_seam_convergence.py`) is disjoint from CAS-baton / autopilot
> / conv_state_ssot — those three epics are explicitly OUT of scope here.

---

## 0. The verdict, in one sentence

ConvView is today the single seam for the **streaming-bubble lifecycle only**; the rest of
the DOM-apply layer is a second tier (`renderChat()` + a large fleet of raw DOM ops) that
still owns ~207 writes. Phase 3.5 makes the DOM apply layer single-seamed: every write to
`#chatInner` either (a) derives from a message-document change via `ConvView`, or (b) is
explicitly classified STRUCT-ONLY (scroll/window management) and allowed to stay, or (c) is
a PENDING placeholder that must NOT read message fields and therefore cannot diverge.

## 1. The three classes (the ONLY taxonomy this plan uses)

Every raw write lands in exactly one of:

| Class | Meaning | Rule |
|---|---|---|
| **CONTENT-DERIVED** | The DOM node *reads a message field* (content / thinking / error / images / igResult / translatedContent / _translatePartial / modifiedFiles / cost / _ctx). | MUST route through ConvView so the DOM is a pure projection of the message. Today raw → the byte-parity test (§3) catches every case where the live write and the cold re-projection diverge. |
| **STRUCT-ONLY** | Scroll / lazy-window / sentinel / placeholder management. The node reads NO message field — it only moves/evicts/inserts DOM slots for performance. | MAY stay raw — it cannot diverge from the SSOT because it never projects content. Explicitly allowlisted so the ratchet test (§4) doesn't flag it. |
| **PENDING-PLACEHOLDER** | A transient "we are waiting" bubble (translating / VLM-wait / image-gen loading). Reads NO message field; its content is a fixed status string. | MAY stay raw for now, BUT it must never read `msg.*`. The moment it shows any message-derived text it becomes CONTENT-DERIVED. Listed so the ratchet covers it. |

The confusion today is that these three classes are **interleaved inside the same functions**
(e.g. `_surgicalRerenderMsg` does BOTH a STRUCT-ONLY re-anchor AND a CONTENT-DERIVED
`el.outerHTML = renderMessage(...)`). Phase 3.5 splits them at the call site.

## 2. The 217-site reclassification table

`N = current raw count` measured 2026-07-24 by the §4 audit (single-pass tokenizer strips
comments + strings, then counts `innerHTML=` / `outerHTML=` / `insertAdjacentHTML(` /
`appendChild(` / `.remove()`). **Census completeness** (step 2): a repo-wide tokenizer scan
of every `static/js/**` file (§2.15) added `streaming_ui.js` (49) and
`core/health_stream_timer.js` (10) to the ratchet and formally exempted `turn_nav.js` /
`finish_info.js` — no third chatInner writer is unaccounted for.
ConvView itself has 10 raw ops (it IS the seam — those are the ALLOWED writes). Every other
file's raw ops are listed below with a class + a Phase-3.5 target.

### 2.1 `static/js/conv_view.js` — N=8 → THE SEAM (keep)

| Line cluster | Op | Class | Target |
|---|---|---|---|
| ~193 | `existing.outerHTML = html` / `inner.insertAdjacentHTML` (**`apply`** — the ONE upsert) | SEAM | keep — step 3 ① collapsed `upsertMessage` into a thin alias of `apply` (legacy append-default-false); the sweep runs on ALL paths |
| ~164 | `el.remove()` (removeMessage) | SEAM | keep |
| ~226 | `inner.insertAdjacentHTML` (startStreaming) | SEAM | keep |
| ~272 | `sm.outerHTML = html` (finalizeStreaming) | SEAM | keep |
| +4 helper ops | evict / restore | SEAM | keep |

### 2.2 `static/js/ui/chat_render.js` — N=8 → THE SEAM'S ENGINE (SEAM-2, step 4 fold)

| Line | Op | Class | Target |
|---|---|---|---|
| 256 | `inner.innerHTML = html` (skeleton render) | SEAM-2 | engine internal |
| 387 | `wrapper.innerHTML = renderMessage(msg, i)` (surgical insert, detached-builder) | SEAM-2 | engine internal |
| 398 | `upd.el.outerHTML = upd.html` (surgical batch swap) | SEAM-2 | engine internal |
| 408, 417 | `el.remove()` / `leftoverStreaming.remove()` | SEAM-2 | engine internal (stale eviction) |
| 457, 460 | `inner.innerHTML = …` (welcome / loading skeleton) | SEAM-2 | engine internal (placeholders) |
| 486 | `inner.innerHTML = html` (full-path wipe) | SEAM-2 | engine internal |

`renderChat`/`_surgicalTruncateDOM` are the seam's reconcile ENGINE: ConvView.replaceAll
delegates here, and their raw writes are the projection IMPLEMENTATION, not a second
public entry. **Step 4 folded the call boundary**: other modules' full repaints route
through `ConvView.replaceAll(convId, {forceScroll})` (16 call sites migrated; the
remaining ~43 across conversations.js / cross_tab_sync.js / health_stream_timer.js /
image-gen*.js / branch.js / core.js / … are the §5 step-5 sweep list). chat_render.js
moves to the SEAM SIDE of the ratchet (pinned at 8, like conv_view's 8) — the ratchet's
job on it is no NEW raw writes beyond the engine's current set.

### 2.3 `static/js/ui/streaming_render.js` — N=21 → the live-write engine (CONVERGE to ConvView)

| Line | Op | Class | Target |
|---|---|---|---|
| 65, 189, 302 | `sm.remove()` (streaming bubble) | STRUCT-ONLY | keep — bubble lifecycle |
| 92 | `el.outerHTML = html` (`_surgicalRerenderMsg`) | CONTENT-DERIVED | `ConvView.apply(convId, idx, msg)` |
| 101, 103 | `insertAdjacentHTML` (VU / after streaming) | CONTENT-DERIVED | `ConvView.apply` (insert-after becomes an apply with an anchor) |
| 203 | `insertAdjacentHTML` (autopilot VU) | CONTENT-DERIVED | `ConvView.apply` |
| 660–662 | `_streamingBubbleHTML` data-msg-id | SEAM-2 | keep — bubble template |
| 714–715 | `el.remove()` (truncate) | STRUCT-ONLY | keep — `_surgicalTruncateDOM` |
| 862–873 | sentinel `s.remove()` / `appendChild` | STRUCT-ONLY | keep — lazy window |
| 891–903 | evict + sentinel `innerHTML`/`insertBefore` | STRUCT-ONLY | keep |
| 948–952, 1065–1069 | lazy-load fragment `innerHTML`/`after`/`before` | CONTENT-DERIVED | these render real messages → route the *message html* through `ConvView.apply`; the fragment insertion itself stays STRUCT-ONLY |
| 1006–1007 | `el.remove()` (evict above window) | STRUCT-ONLY | keep |

### 2.4 `static/js/ui/sse_pipeline.js` — N=17 → the live event apply (CONVERGE)

| Line | Op | Class | Target |
|---|---|---|---|
| 434, 884, 1333, 1416, 1561, 1619 | `insertAdjacentHTML(_streamingBubbleHTML…)` (ConvView fallback) | CONTENT-DERIVED | delete the raw fallback — ConvView is always present after Phase 3.5; keep only the `ConvView.startStreaming` call |
| 456 | `_body.innerHTML = _html` (resume partial) | CONTENT-DERIVED | `ConvView.apply` on the resumed assistant msg |
| 741, 1279, 1281 | planner avatar/body `innerHTML` | CONTENT-DERIVED | `ConvView.apply` on the planner msg |
| 872 | `staleRenderedEl.remove()` | STRUCT-ONLY | keep |
| 1379, 1649 | `existingSm.remove()` / `danglingSm.remove()` | STRUCT-ONLY | keep |
| 1474, 1480 | `sm.outerHTML = renderMessage(...)` (dangling planner) | CONTENT-DERIVED | `ConvView.apply` |
| 2041 | `_sm.remove()` (ConvView fallback) | STRUCT-ONLY | delete the raw fallback |

### 2.5 `static/js/main/main_send_pipeline.js` — N=16 (was 23) → CONVERGED in step 3

| Line | Op | Class | Target | State |
|---|---|---|---|---|
| 446 | `insertAdjacentHTML(renderMessage(userMsg,…))` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 |
| 583, 705, 1040 | `msgEl.outerHTML = renderMessage(userMsg,…)` (server-translated / abort-restore / VLM done) | CONTENT-DERIVED | `ConvView.apply` (existence-check kept) | ✅ step 3 |
| 611, 639 | `msgEl.remove()` (steer/queue splice) | CONTENT-DERIVED | `ConvView.removeMessage` | ✅ step 3 |
| 756 | `insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 |
| 1011–1045 | VLM-wait indicator `appendChild` + `innerHTML` + `remove()` | PENDING-PLACEHOLDER | keep — fixed status string, no msg field | keep |
| 230, 923, 1380, 1402 | `oldSm/_ghost.remove()` | STRUCT-ONLY | keep | keep |

### 2.6 `static/js/main/main_regen_continue.js` — N=2 (was 4) → CONVERGED in step 3

| Line | Op | Class | Target | State |
|---|---|---|---|---|
| 149 | `msgEl.outerHTML = renderMessage(msg, idx)` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 |
| 215 | `insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 |
| 411 | `hdr.appendChild(tmEl)` (Continue elapsed timer) | STREAMING-LIFECYCLE | keep — this path renames `msg-${lastIdx}` INTO `#streaming-msg` and builds the live zones; it belongs to the startStreaming/finalize family, and step-3 ② makes `apply` REFUSE the live bubble by construction | reclassified |
| 417 | `bodyEl.innerHTML = …` (Continue zones) | STREAMING-LIFECYCLE | same as 411 | reclassified |

### 2.7 `static/js/main/main_translating_bubble.js` — N=6 → PENDING-PLACEHOLDER (keep, guarded)

| Line | Op | Class | Target |
|---|---|---|---|
| 37, 47 | `el.innerHTML` / `inner.appendChild(el)` (translating bubble) | PENDING-PLACEHOLDER | keep — fixed '翻译中…'/'连接中…' string |
| 55 | `el.remove()` | PENDING-PLACEHOLDER | keep |
| 74 | `insertAdjacentHTML` (streaming fallback) | CONTENT-DERIVED | delete raw fallback (ConvView always present) |

### 2.8 `static/js/ui/edit_message.js` — N=1 (was 7) → CONVERGED in step 3

| Line | Op | Class | Target | State |
|---|---|---|---|---|
| 52 | `bodyEl.innerHTML = …edit form…` | INTERACTIVE-EDITOR (named exception) | keep — the edit form (textarea + buttons + paste handlers) is an interactive widget `renderMessage` does not produce; `apply` would destroy focus/selection on every keystroke-adjacent repaint. Named here as a permanent exception | keep |
| 143, 263, 279, 384, 484 | `msgEl.outerHTML = renderMessage(msg, idx)` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 (manual fingerprint refreshes removed — apply does it) |
| 550 | `insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` | ✅ step 3 |

### 2.9 `static/js/ui/translation_render.js` — N=17 (was 18) → CONVERGED in step 2 (the "translation lands but UI doesn't refresh" bug home)

| Line | Op | Class | Target | State |
|---|---|---|---|---|
| 64 | `el.outerHTML = renderMessage(msg, idx)` (`_renderMsgInPlace`) | CONTENT-DERIVED | `ConvView.apply` | **✅ step 2** — routes through `ConvView.apply` (cv-off scroll dance kept; no raw fallback — §5 step-4 boot check guarantees the seam) |
| 95, 100, 107, 112–113, 131, 137 | translate-loading patch (status / preview into `#translate-loading-N`) | CONTENT-DERIVED | **sanctioned surgical exception** — a full `ConvView.apply` per poll tick would restart the `.translate-spinner` CSS keyframe (the exact reason `_patchTranslateLoadingDom` exists); the completion path always ends in a whole-bubble `ConvView.apply`, so the indicator is a transient sub-projection of the same msg | keep, named here |
| 184, 192 | streaming narration `innerHTML` | CONTENT-DERIVED | `_renderStreamingTranslatePreview` — live in-bubble sub-projection; **step 2 made its zh node byte-identical to the settled slot** (dropped `stream-seg-narration`; visuals unchanged via `.seg-timeline .seg-narration`, styles.css:6096 ≡ :6158 values) | ✅ parity landed |
| 212, 217, 232, 240, 277, 285 | settled per-round narration `innerHTML` | CONTENT-DERIVED | `_applyPartialByRoundToSettled` — the COLD side of the §3 anchor; surgical per-round paint is its purpose (a whole-bubble apply would defeat retro-translation streaming) | keep, named here |

This file is the single biggest CONTENT-DERIVED cluster and the direct cause of the
"translation lands but the UI doesn't refresh" bug family. Step 2 made its whole-bubble
path (`emitMessageChanged` → `_renderMsgInPlace`) call `ConvView.apply`, and made the
live preview's zh narration byte-identical to the cold render (the §3 anchor, now GREEN).

### 2.10 `static/js/image-gen.js` — N=18 + `image-gen-batch.js` — N=5 → mixed

| Line | Op | Class | Target |
|---|---|---|---|
| image-gen.js:311, batch:127 | `chatDiv.insertAdjacentHTML(loadingHtml/gridHtml)` | PENDING-PLACEHOLDER | keep — fixed loading markup |
| image-gen.js:364, 392, 418, batch:262 | `loadingEl.remove()` / `footerEl.remove()` | PENDING-PLACEHOLDER | keep |
| batch:173, 499, 526, 552, 569, 202, 241 | `slotEl.innerHTML = ig-result / ig-error` | CONTENT-DERIVED | reads `msg._igResult(s)` / `_igError` → the result card becomes a projection of the msg via `ConvView.apply` |
| image-gen.js:172, 188, 629, 672, 685 | model-picker dropdown `innerHTML` | NOT-#chatInner | out of scope (this is the picker UI, not a chat bubble) — exclude from the ratchet |

### 2.11 `static/js/ui/stream_lifecycle.js` — N=6 → mostly STRUCT

| Line | Op | Class | Target |
|---|---|---|---|
| 116 | `inner.innerHTML = html` (welcome / reset) | PENDING-PLACEHOLDER | keep |
| 250, 254, 366, 384 | `sm/_sm.remove()` (streaming bubble) | STRUCT-ONLY | keep |
| 608 | `insertAdjacentHTML` (queued-dispatch placeholder) | PENDING-PLACEHOLDER | keep |

### 2.12 `static/js/main/main_conv_lifecycle.js` — N=10 → mostly NOT-#chatInner / placeholder

| Line | Op | Class | Target |
|---|---|---|---|
| 53 | `topbarEl.innerHTML` | NOT-#chatInner | exclude (topbar, not a bubble) |
| 63 | `chatInner.innerHTML = welcome` | PENDING-PLACEHOLDER | keep |
| 548, 562, 568, 571 | toast `innerHTML`/`appendChild`/`remove` | NOT-#chatInner | exclude (toast overlay) |
| 696, 701, 715, 721 | modal overlay `innerHTML`/`appendChild`/`remove` | NOT-#chatInner | exclude (modal) |

### 2.13 `static/js/core/conversations.js` — N=4 → PENDING-PLACEHOLDER (keep)

| Line | Op | Class | Target |
|---|---|---|---|
| 1747, 1767, 2300 | `inner.innerHTML = …error welcome…` | PENDING-PLACEHOLDER | keep — fixed error string, no msg field |

### 2.15 Census join (step 2) — `streaming_ui.js` + `health_stream_timer.js`, and the exemption register

The first-pass table missed the hottest writer of all. A repo-wide tokenizer census
(every `static/js/**` file, `*.nc_copy.js` and vendor libs excluded) proved the full set of
chatInner writers and closed the "ratchet is blind on the hottest file" hole.

#### 2.15.1 `static/js/ui/streaming_ui.js` — N=49 → the live projection engine (JOINED the ratchet)

| Line cluster | Op | Class | Target |
|---|---|---|---|
| ~31 | `body.innerHTML = …` (seed zone skeleton in `#streaming-body`) | CONTENT-DERIVED (live) | live-projection engine — see below |
| ~293, ~330 | `tailEl.innerHTML = renderMarkdown(tail)` | CONTENT-DERIVED (live) | same |
| ~324, ~1000 | `insertAdjacentHTML` (refreeze / turn headers) | CONTENT-DERIVED (live) | same |
| ~508–596 | `_renderStreamRoundProse` — think/narration `innerHTML` + `remove()` | CONTENT-DERIVED (live) | already byte-parity-claimed with the settled `_renderTimelineBatch`; the §3 anchor covers its narration twin |
| ~740–900 | tool panel / group / slot `appendChild` + `innerHTML` | CONTENT-DERIVED (live) | same |
| various | sentinel / evict `remove()` | STRUCT-ONLY | keep |

The 2026-08-14 program-call projection kept this file at its existing ratchet
floor: the `execute_tools` gateway reuses the superseded-round prune owner,
and two duplicate streaming-tail assignment sites were consolidated to offset
the settled-thinking Markdown paints. No additional raw writer was admitted.

**Classification verdict: CONTENT-DERIVED (live-projection engine).** Its zones project the
SAME `assistantMsg` the reducer mutates — the divergence risk is not *what* it reads but
*where the write comes from* on non-SSE triggers (`health_stream_timer._streamFrameArg`,
poll fallback, VU events — the §7 streamBufs retirement). Per-frame `ConvView.apply` is
explicitly REJECTED as the convergence target: a full `renderMessage` per rAF is a perf
regression the lazy-window machinery exists to avoid. Convergence path for this file =
§7 (its reads move onto the message document) + the ratchet (no NEW raw writes), not
apply-per-frame.

#### 2.15.2 `static/js/core/health_stream_timer.js` — N=10 → liveness banner inside the bubble (JOINED)

| Line cluster | Op | Class | Target |
|---|---|---|---|
| ~910–912 | `statusZone.appendChild(banner)` + `banner.innerHTML` (liveness warning inside `#streaming-body`) | CONTENT-DERIVED (reads `buf.phase`) | §7 retirement — read `phase` from the message document / reducer, not `streamBufs` |
| various | timer text / cleanup `remove()` | STRUCT-ONLY | keep |

#### 2.15.3 Exemption register (census-verified, with reasons)

| File | Raw ops | Verdict | Evidence |
|---|---|---|---|
| `static/js/ui/turn_nav.js` | 7 | **EXEMPT** — other-root + detached-builder | writes target `#turnNav` (sidebar, L50/84/87); L141 `wrapper.innerHTML` only parses an HTML string into a detached fragment for lazy-load prepend — no chatInner mutation via the 4 audited patterns |
| `static/js/ui/finish_info.js` | 0 | **EXEMPT** — zero chatInner writes | tokenizer finds no raw ops; the cost popover appends to `document.body` (L663), not `#chatInner` |

Any OTHER `static/js/**` file with tokenizer count > 0 either sits in the §2 tables or is
listed here — the census is closed-world: a new file gaining a chatInner write fails the
ratchet only after it is added to `_RATCHET_BASELINE`, so **adding new files to the ratchet
when they first gain a chatInner write is part of every future step's checklist.**

### 2.14 Tally

| Class | Count | Phase-3.5 action |
|---|---|---|
| SEAM (conv_view.js) | 10 | keep — this IS the seam (includes `apply`, landed step 2) |
| SEAM-2 (chat_render reconcile) | ~7 | fold call boundary into `ConvView.apply`/`replaceAll` |
| CONTENT-DERIVED | ~132 (incl. streaming_ui live-engine ~45 + health_stream_timer banner ~2) | route through `ConvView.apply` — or, for the live projection engine, §7 retirement of its second data source; the byte-parity test (§3) is the guard |
| STRUCT-ONLY | ~34 | keep, explicitly allowlisted |
| PENDING-PLACEHOLDER | ~18 | keep, must never read `msg.*` |
| NOT-#chatInner | ~16 | excluded from class rules, still under the ratchet |
| **Total raw ops (tokenizer v3)** | **173 = 16 seam (8 conv_view + 8 chat_render engine) + 157 non-seam** (post-step-4) | seam+struct+placeholder stay + **CONTENT-DERIVED converges** |

Exact per-file numbers live in `_RATCHET_BASELINE` (the guard); the class split above is
the §2 tables' cluster-level rollup (±3).

## 3. The failing-first byte-parity test (the Phase-3 acceptance anchor, generalized to DOM)

`tests/test_frontend_dom_seam_convergence.py` — JSDOM harness.

**The claim under test:** for ONE logical turn, the DOM produced by folding the LIVE event
stream (delta → tool rounds → translation preview → done) MUST be byte-identical to the DOM
produced by the COLD reload of the same settled message.

```
LIVE  : events  → reduceStreamState → (today: raw DOM writes)      → liveHTML
COLD  : settled msg → renderMessage(msg, idx)                       → coldHTML
ASSERT: liveHTML === coldHTML        // byte-identical #chatInner subtree
```

**RED at step 1** because the ~85 CONTENT-DERIVED raw writes (~132 after the step-2 census
joined `streaming_ui.js` / `health_stream_timer.js`) project message fields through a
DIFFERENT code path than `renderMessage` — the concrete first instance: the live
translation-preview zh node (`_renderStreamingTranslatePreview`) carried an extra
`stream-seg-narration` class that the cold settled render
(`tool_rounds.js:_renderSegNarrationHTML`) never emits.

**GREEN for the narration anchor as of step 2:** the LIVE painter changed to the settled
class contract (`md-content seg-narration`; visuals unchanged — the live panel carries
`seg-timeline`, so `.seg-timeline .seg-narration` at styles.css:6096 applies, values
identical to the now-inert `.stream-seg-narration` block at :6158, whose cleanup is
deferred to the CSS-owning sibling). The test's side-pin check
(`live_class_is_the_settled_contract`) locks WHICH side moved, so the anchor can't rot in
either direction. The remaining CONTENT-DERIVED sites flip GREEN as §5 steps 3–4 route
them through `ConvView.apply`.

**NEUTERs (all real, all in `tests/test_frontend_dom_seam_convergence.py`):**
1. `NEUTER_injected_byte_difference_detected` — embedded in the JSDOM harness: mutating
   the cold slot by one byte must be detected by the comparator.
2. `test_NEUTER_ratchet_detects_injected_raw_op` — poisoning a ratcheted file's source
   with one extra raw op must increment its count.
3. Step-2 NEUTER **round-trip** (run manually at landing, reproduced in the commit
   message): a scratch copy of `translation_render.js` with the old
   `stream-seg-narration` class re-added flips the anchor RED (both the byte anchor and
   the side-pin trip) — proving the GREEN is load-bearing, not vacuous.

## 4. The ratchet guard (no NEW raw writes)

A second test in the same file statically audits the 15 non-seam files (tokenizer strip,
count the 4 raw-DOM op patterns), asserting each file's count is **≤ its §2 baseline** —
the monotonic-decrease ratchet (same pattern as `test_frontend_api_isolation.py`): the
baseline only ever goes DOWN as sites converge; any NEW raw write fails CI.
Baselines (2026-07-24 post-step-4, tokenizer **v3** — strings replaced with a non-empty
placeholder so `classList.remove('cv-off')` no longer counts as a DOM detach):
streaming_ui 49, streaming_render 20, translation_render 14, image-gen 13,
main_send_pipeline 12, health_stream_timer 10, main_conv_lifecycle 10, sse_pipeline 10,
stream_lifecycle 5, image-gen-batch 5, main_translating_bubble 3, core/conversations 3,
main_regen_continue 2, edit_message 1. SEAM SIDE (pinned): conv_view 8, chat_render 8.
Sum(non-seam) = 157.

## 5. Landing order (each step committable + tested; mirrors Phase 3 §7)

1. **This plan + failing-first test skeleton (RED).** ✅ LANDED (`d0ec8dca`).
2. **`ConvView.apply` + translation_render convergence + census join + boot hard check.**
   ✅ LANDED (this commit): `ConvView.apply(convId, idx, msg)` = `renderMessage` +
   identity sweep (`_evictByMsgId`) + fingerprint refresh; `_renderMsgInPlace` routes
   through it; the narration byte-parity anchor flipped GREEN (live side moved to the
   settled class contract); ratchet joined by `streaming_ui.js` (49) +
   `health_stream_timer.js` (10); **the step-4 boot-check precondition landed EARLY**
   (see below); translation_render 18→17. NEUTER round-trip verified.
3. Route the send/regen/edit single-message swaps (§2.5/2.6/2.8) through `ConvView.apply`.
   ✅ LANDED (this commit) — plus the four owner-directed seam-hardening items:
   **① collapse** — `apply(convId, idx, msg, opts)` is the ONLY upsert entity;
   `upsertMessage` is a thin alias preserving legacy append-default-false semantics;
   the identity sweep runs on ALL paths (guards: static delegation check +
   runtime parity incl. twin-eviction).
   **② live-bubble guard** — `apply` REFUSES (console.warn + false) when its resolved
   target is/inside `#streaming-msg` (per-round auto-translate completes mid-stream,
   so this was a real footgun, not hygiene); `_evictByMsgId` never removes
   `#streaming-msg` (NEUTER: deleting the guard lets apply destroy the live bubble).
   **③ order invariant** — (a) apply loudly warns when appending a non-tail message
   (index-drift surface); (b) JSDOM anchor: after a send → edit → regen → upsert
   flow through the seam, `#chatInner .message` data-msg-id sequence ===
   `conv.messages` `_msgId` sequence (the cheapest hard proof of traceable rendering).
   **④ retired-class sweep** — the inert `.stream-seg-narration` CSS block deleted
   from styles.css (step 2's byte-parity fix killed its class); static guards pin:
   no production JS (comments stripped) may carry the token, no CSS rule may
   reference it. Ratchet 207 → 192.
4. SEAM-2 fold + raw-fallback deletion + boot-check RUNTIME proof. ✅ LANDED:
   (a) `ConvView.replaceAll(convId, {forceScroll})` is THE public full-repaint entry —
   16 call sites across 9 files migrated off bare `renderChat(...)` (translation_render
   ×2, edit_message ×3, send_pipeline ×3, regen_continue ×3, sse_pipeline ×1,
   stream_lifecycle ×1, streaming_render ×2, conv_lifecycle ×2); chat_render.js declared
   the seam's ENGINE (pinned at 8 on the seam side of the ratchet).
   (b) ALL `window.ConvView`-missing raw fallbacks deleted — 6 `_streamingBubbleHTML`
   else-branches in sse_pipeline.js, 1 in main_send_pipeline.js, 1 in
   main_translating_bubble.js, plus 6 presence-guard patterns (upsert/removeMessage/
   finalizeStreaming) flattened to direct calls; 2 now-unused `inner` declarations
   swept. Static guards pin: no `typeof window.ConvView.` guard, no
   `_streamingBubbleHTML` else-branch, no bare `renderChat(` in migrated files.
   (c) The boot check's RUNTIME proof (owner's evidence-gap item): JSDOM evals main.js
   with `window.ConvView` undefined → the fixed banner is IN THE DOM + console.error
   captured (pre-eval wrap); NEUTER: a ConvView stub → no banner, no error. Static
   existence ≠ runtime trigger — now both halves are proven.
   **Precondition (LANDED in step 2): the boot-time ConvView hard check** —
   `main.js` init checks `typeof window.ConvView?.apply === 'function'` and, on
   absence, fires `console.error` + pins a fixed banner. Guard:
   `test_boot_hard_check_convview_present` (static) + the two runtime tests above.
   Ratchet: 192 → **157** (fallback deletions + scanner v3 accuracy + chat_render to
   the seam side).
5. ✅ LANDED (this commit): all 43 bare `renderChat(` sites across 21 files swept onto
   `ConvView.replaceAll` (the last one was image-gen.js's catch-block error render);
   message_actions.js's translate-toggle `outerHTML` also converged onto `apply`.
   **Fold guard upgraded to GLOBAL ZERO** (owner condition 1): no bare `renderChat(`
   anywhere in `static/js/**` outside the two seam files (conv_view.js + chat_render.js,
   named individually — no pattern exemption); the exemption register is EMPTY because
   even turn_nav.js / finish_info.js were migrated. **§7.4 RED anchor landed in the
   same commit** (owner condition 3 — failing-first evidence locked into history before
   the §7 retirement).
   §7 (the streamBufs retirement) and §5-final (the allowlist freeze) both LANDED
   after this step — see §7 + the §5-final note below.

### §5-final — the allowlist freeze (LANDED)

The ratchet baseline is now the PERMANENT FLOOR: the remaining STRUCT-ONLY +
PENDING-PLACEHOLDER sites (§2 tables, `keep`/`sanctioned exception` rows) are the
declared allowlist — no further convergence is planned, and any NEW raw write above
the floor fails the ratchet. The live projection engine (streaming_ui.js) is the one
sanctioned CONTENT-DERIVED exception: its per-frame writes project the SAME
assistantMsg the reducer mutates (a full `renderMessage` per rAF is a perf regression
the lazy-window machinery exists to avoid), and its second data source (streamBufs)
was retired in §7, so it now reads the document + streamSessions only.

**streamBufs-v2 door closed (owner §7-验收 cond 1):** the session key contract is
guarded — a `streamSessions` entry may carry ONLY `{phase}` (any `content`/`thinking`/
`toolRounds` key beside the document re-opens the second-fact-source treadmill). Guards:
`test_stream_session_keys_are_phase_only` (static, NEUTER: injected `session.content`
flips red) + `test_stream_session_reader_surface_pinned` (read surface = exactly the
3-file / 5-site allowlist, matching the stream_session.js doc header).

## 6. Boundary

- **In scope:** `static/js/conv_view.js`, `static/js/ui/{chat_render,streaming_render,sse_pipeline,sse_handlers_*,stream_lifecycle,edit_message,translation_render}.js`, `static/js/main/main_{send_pipeline,regen_continue,translating_bubble}.js`, `static/js/{image-gen,image-gen-batch}.js`, `static/js/core/conversations.js`, `lib/js_bundler.py`.
- **Out of scope (explicitly):** CAS-baton / autopilot (`lib/tasks_pkg/autopilot*`),
  conv_state_ssot (`lib/agent_core/push.py`, `routes/push.py`, `conv_state_reducer.js`),
  `lib/agent_core/events.py` (Phase 3's wire contract — separate sign-off), and the DB
  (`_sync_*` writers — Phase 4). The VU render in `streaming_render.js:428-491` is listed
  here ONLY for its DOM-write classification; its baton semantics are untouched.

---

## 7. streamBufs disposition — the second live fact-source, enumerated and judged

**STATUS: RETIRED (this commit).** `streamBufs` is deleted (the core.js declaration is
gone); a fresh re-census at retirement time found 21 references across 10 files — every
one is migrated per the dispositions below, and the static guard
(`test_streambufs_fully_retired`) pins zero references in production JS code forever.
**Acceptance semantics (owner, written in stone): the §7.4 anchor promises byte parity
at the CHECKPOINT BOUNDARY only — between two checkpoints the document is allowed to
lag the live accumulation, because checkpointing cadence is a persistence contract,
not a render-source divergence.**

`conv.messages` is the SSOT, but `streamBufs` (declared `core.js:150`) is a **parallel
live content source**: SSE deltas write it, and several readers paint from it OUTSIDE the
SSE-delta path. Hard rule (owner, 2026-07-24): **anything read outside the SSE-delta path
to mutate the DOM is a second fact source** — it must get a retirement path or an explicit
exemption argument. Full census (every `streamBufs` access, read vs write, outcome):

### 7.1 PAINT-SOURCE — ALL RETIRED ✓ (8 readers)

| Site | What it paints | How it retired |
|---|---|---|
| `health_stream_timer.js:814` `_updateStreamTimerUI` banner | banner | reads `streamSessions.get(cid).phase` |
| `health_stream_timer.js:996` `_streamFrameArg` | whole live bubble | frame built from the DOCUMENT + `sess.phase` (no buffer at all) |
| `sse_pipeline.js` `_trySSE` closure — deltas wrote buf → `twUpdate` | whole live bubble | mirror deleted; deltas mutate the doc only; phase → `setStreamPhase` |
| `sse_poll_fallback.js:59` poll wrote buf | whole live bubble | poll mutates the doc only; `data.phase` → `setStreamPhase` |
| `stream_lifecycle.js:138` + `:171` `showStreamingUIForConv` / deferred re-render | reconnect repaint | reads the document + `sess.phase` |
| `stream_lifecycle.js:660,729,744` HG-translate sync | tool rounds | mirror deleted — pipeline reads the doc directly |
| `streaming_render.js:414` VU event | VU bubble | VU deltas mutate `vuMsg` (doc); phase mirror → `setStreamPhase` |
| `project.js:178` `_collapseHgRoundAfterSubmit` | tool rounds | mirror deleted — stamp on the doc is visible to the pipeline |

**Unified retirement sketch (one mechanism, 8 sites):** `streamBufs` fields are mirrors of
`assistantMsg` fields (every delta writes BOTH in the same handler). Delete the buffer as a
READ source: `twUpdate`/`_streamFrameArg`/`showStreamingUIForConv` project from
`assistantMsg` (the doc); writers stop mirroring. The buffer's only remaining legitimate
role — an existence flag for "a stream is live on this conv" — is already served by
`activeStreams` / the task registry. This lands as its own step between §5.4 and §5.5, with
the §3 byte-parity anchor extended to the reconnect path (cold-open vs live-paint of an
in-flight turn).

### 7.2 BUFFER-ONLY — ALL RETIRED ✓ (5 readers)

| Site | Retired as |
|---|---|
| `health_stream_timer.js` presence checks ×3 | `activeStreams.has(cid)` (the liveness registry — the session's own live-only guard also consults it) |
| `sse_pipeline.js:481,519` `connectToTask` buf seed | deleted entirely — the document IS the checkpoint; nothing to seed |
| `cross_tab_sync.js:588` `_revalidateOnResume` presence check | `activeStreams.has(cid)` |
| `conversations.js:2036` `_rebaseUnackedTail` buf seed | deleted — `showStreamingUIForConv` reads the document |
| `main_send_pipeline.js:964` `console.info` diagnostic | reports `streamSessions.has(cid)` instead |

### 7.3 The retirement itself (LANDED — this commit)

- **Entity home for phase** (owner §7 cond 2): `static/js/ui/stream_session.js` —
  `var streamSessions = new Map()` + `getStreamSession` (lazy `{phase:null}`) +
  `setStreamPhase` (**live-only guard**: no-op unless the session exists or
  `activeStreams.has(cid)` — a post-stop phase event cannot resurrect a session) +
  `clearStreamSession` (called by `twStop` and every bubble-teardown path).
- **Re-seed evidence** (owner §7 cond 2, verdict A for warm resume):
  `lib/chat_dispatch.py:636` — a warm reconnect with `Last-Event-ID` replays
  `task['events'][cursor:]`, which INCLUDES the latest PHASE event, through the same
  `dispatchSSEEvent` path → `setStreamPhase` re-seeds the session exactly as production.
  A fresh cursorless connect accepts the transient "phase null until the next live
  PHASE event" (verdict C) — the default waiting pulse, which is truthful (the client
  genuinely does not know what the model is doing yet).
- **Static guards** (owner §7 cond 3): `test_streambufs_fully_retired` (zero references
  in production JS code — anti-treadmill) + `test_stream_session_module_contract`
  (the entity exists, the live-only guard exists, the writer allowlist = sse_pipeline
  / sse_poll_fallback / streaming_render, `_BUNDLE_FILES` membership).
- **Anchor flipped** (owner §7 cond 4): the §7.4 anchor is GREEN — content/thinking/
  status zones byte-identical across live vs warm-reconnect, PLUS the live-bubble
  identity check (`id="streaming-msg"` + `data-msg-id` stable across arms).

### 7.4 The reconnect byte-parity anchor (RED anchor LANDED in step 5; flips GREEN with the §7 retirement)

**Owner ruling on the phase home (2026-07-24, step 5 condition 2 — WRITTEN IN STONE):**
`phase` belongs to the **reducer's live session state — NEVER the message document.**
It is runtime state (what the model is doing right now), not turn content; writing it
into `conv.messages` would pollute the SSOT with runtime state and contradict "no second
fact source" from the other direction. The §7 retirement therefore moves phase OUT of
`streamBufs` and INTO the reducer's live session state, and every reader
(`health_stream_timer._updateStreamTimerUI` banner, `_streamFrameArg`) reads it from
there. The reconnect arm reads the same session state — that is what makes the status
zone byte-parity structural instead of discipline-maintained.

**Anchor coverage EXTENDED to the status zone (owner condition 2):** the byte
comparison covers content + thinking + **status zone** — the only place `phase`
shows. A content-only anchor could never prove the phase migration correct.

**Anchor status: RED, landed in step 5** (`tests/test_frontend_reconnect_parity_anchor.py`),
JSDOM over the REAL `_streamFrameArg` (health_stream_timer.js) + `updateStreamingUI`
(streaming_ui.js). Current verdict: content zone byte-identical ✓, thinking zone
byte-identical ✓ (the checkpoint fallback covers both — the anchor pinpoints the gap),
**status zone DIVERGES** (live arm paints the `llm_thinking` phase block; the
reconnect arm's doc-seeded buffer has no phase → default waiting pulse). NEUTER:
mutating the checkpoint content flips the content check red (comparator load-bearing).
The §7 retirement flips the status zone GREEN by giving both arms the same phase
source.

**Claim under test:** for ONE in-flight turn, the `#chatInner` subtree produced by a
COLD-OPEN RECONNECT (open the conv mid-stream → `connectToTask` replays the persisted
checkpoint + `showStreamingUIForConv` paints the live bubble) MUST be byte-identical to
the subtree produced by the LIVE-PAINT path (the tab that held the SSE stream from the
first delta) at the SAME logical instant — **content zone, thinking zone, AND status
zone** (see the ruling above).

**Why this is the §7 acceptance anchor:** today the two paths paint from DIFFERENT
sources — live paints from `streamBufs` (SSE deltas), reconnect paints from
`conv.messages` (the persisted checkpoint) with `streamBufs` re-seeded FROM it. If both
projected from the message document alone, byte-parity would be structural; today it
holds only by the "every delta double-writes buf + msg" discipline. The anchor is what
proves the §7 retirement (buf deleted as a read source, `twUpdate`/`_streamFrameArg`/
`showStreamingUIForConv` projecting from the doc) changed NOTHING observable.

**Harness sketch (JSDOM, mirrors the §3 narration anchor):**
1. Build a settled-in-progress doc state: `assistantMsg` with content/thinking/toolRounds
   up to checkpoint K (same shape `connectToTask`'s `_trySSE` checkpoint fallback uses).
2. LIVE arm: feed the same turn's event log (delta → tool rounds → thinking) through the
   live paint path (`updateStreamingUI` reading the buffer as filled by deltas), snapshot
   `#streaming-body.innerHTML` at checkpoint K.
3. RECONNECT arm: from ONLY the message document at checkpoint K, run
   `showStreamingUIForConv` + the frame-arg builder, snapshot the same subtree.
4. ASSERT byte-equality of the two subtrees (with the same fixed clock / id stubs the
   §3 anchor uses). NEUTER: mutate ONE field in the doc between arms (e.g. append a
   thinking char) — the comparator MUST flag the diff, proving the anchor is
   load-bearing, not vacuous.

**What counts as "the same logical instant":** the checkpoint boundary — the exact
content/thinking/toolRounds triple persisted at the last `_last_checkpoint` write. The
live arm's buffer and the reconnect arm's doc are compared there, never mid-delta (a
mid-delta comparison would be a race, not a contract).

**Ordering:** this anchor is written FIRST (RED, proving today's two paths diverge
somewhere — e.g. the reconnect arm's `phase`/status-zone reconstruction), then the §7
retirement lands and flips it GREEN by making both arms read the same document. Same
failing-first discipline as §3.
