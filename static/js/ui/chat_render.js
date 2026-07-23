/* ═══════════════════════════════════════════════════════════════════
   chat render — extracted from ui.js (split 2026-05-28)

   renderChat() + renderMessage() + per-message fingerprint diffing.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-message fingerprint for surgical DOM diffing.
 *  Must change whenever the rendered HTML for this message would differ. */
/* ── Autopilot run fold ───────────────────────────────────────────────
 * Collapse a CONCLUDED autopilot run's VU<->agent transcript into one
 * <details>, leaving the original human message (above, un-stamped) and the
 * close-out summary (below) as the visible spine — "human msg → report".
 *
 * Grouping keys on the EXPLICIT `data-ap-run` id stamped by renderMessage
 * (VU turns + summary) — never role-scanning. The interleaved worker
 * assistant turns between VU turns are NOT individually stamped, so we fold
 * the contiguous DOM sibling RANGE from the run's first stamped element up to
 * (but excluding) its `data-ap-summary` element. A run is "concluded" iff its
 * summary element is present in the DOM; an in-progress run (no summary yet)
 * stays fully expanded and live.
 *
 * Idempotent + surgical-safe: re-running over already-folded DOM is a no-op
 * (the inner turns live inside `.autopilot-run-fold` and are skipped). The
 * <details> open/closed state is preserved across re-renders via a per-run
 * memory so a forced re-render doesn't snap an expanded run shut.
 */
const _apFoldOpenState = {};

/* Look up a run's sidecar record (human-only, NOT a chat message). ONE record
 * per run carries BOTH the terminal fact (status:'concluded', reason) AND the
 * optional close-out report (content — absent on a manual stop). Stored
 * backend-side under settings.autopilotSummaries[runId] and mirrored onto
 * conv.autopilotSummaries by _applySettingsToConv / _handleAutopilotRunConcluded.
 * Returns {runId, status, reason, content?, translatedContent?, ts, _summaryId}
 * or null. */
function _apRunRecord(conv, runId) {
  if (!conv || !conv.autopilotSummaries || !runId) return null;
  const rec = conv.autopilotSummaries[runId];
  return (rec && typeof rec === 'object') ? rec : null;
}

/* ★ BACKEND-AUTHORITATIVE "run has concluded" — the frontend NEVER infers
 * run-end from stream / task / pending-carrier state anymore (that inference
 * WAS the inter-turn-gap mis-fold bug: between turns the stream is briefly
 * gone AND activeTaskId briefly null, so "nothing in flight" was ALSO true
 * mid-run → premature fold). A run folds iff:
 *   (a) the backend wrote a concluded record for it
 *       (conv.autopilotSummaries[runId].status==='concluded'), delivered via
 *       the autopilot_run_concluded SSE, the disarm response, or the settings
 *       round-trip — this covers BOTH a clean [VU: TASK_DONE] (reason=task_done,
 *       with a report) AND a manual Stop / toggle-off / supersede (reason=
 *       stopped, no report); OR
 *   (b) a NEWER run exists after it (structurally over — the backend stamps
 *       _autopilotRunId on every persisted VU turn, so a later run id in the
 *       DOM list is itself a backend fact, not a client guess).
 * The inter-turn gap needs no special-casing now: no concluded record is
 * written between turns, so the last run simply stays fully expanded until the
 * backend says the whole run ended. (A legacy pre-migration record that has
 * `content` but no `status` still folds — treat report-presence as concluded.) */
function _apRunConcluded(conv, runId, isLast) {
  const rec = _apRunRecord(conv, runId);
  if (rec && (rec.status === 'concluded' || rec.content)) return true;  // (a) backend fact
  if (!isLast) return true;                                             // (b) superseded
  return false;
}

/* ★ FLATTENED (owner directive 2026-07-07): autopilot runs are NO LONGER
 * collapsed into a `<details>` card. VU turns render as a flat sequence
 * identical to a normal agent chat, so each turn keeps its own hover action
 * bar (copy / translate / DELETE) — the run-fold was the sole reason a
 * concluded VU turn's delete button was unreachable. This function is kept
 * (3 render call-sites + the run-concluded sidecar plumbing still invoke it)
 * but now does the OPPOSITE of folding: it defensively UNWRAPS any stale
 * `.autopilot-run-fold` left in the DOM (e.g. from a surgical re-render that
 * predates this change), hoisting its turns back to the flat sibling level so
 * the layout never gets stuck half-folded. */
function _applyAutopilotRunFolds(inner, conv) {
  if (!inner) return;
  try {
    inner.querySelectorAll('details.autopilot-run-fold').forEach(fold => {
      const parent = fold.parentNode;
      if (!parent) return;
      /* Move every real message turn out of the fold, before the fold node,
       * preserving order; drop the fold's own <summary> chrome. */
      fold.querySelectorAll(':scope > .message').forEach(msgEl => {
        parent.insertBefore(msgEl, fold);
      });
      parent.removeChild(fold);
    });
  } catch (e) {
    console.warn('[Autopilot fold] unwrap failed (non-fatal):', e && e.message);
  }
}

/* ── RENDER_CONTRACT Invariant 3: content HASH, not length ────────────────
 * A tiny non-cryptographic string hash (FNV-1a, 32-bit, base-36 encoded).
 * The per-message content version below hashes `content`/`thinking` through
 * this so an EQUAL-LENGTH edit (same length, different bytes) moves the
 * version and repaints — the L1 bug the old `String(field).length` compare
 * silently dropped. Cheap: one pass per string, called ~once per rendered row. */
function _hashStr(s) {
  s = s == null ? '' : String(s);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h.toString(36);
}

function _msgFingerprint(msg) {
  const sr = msg.toolRounds || msg.searchResults;
  /* Count compacted rounds so a tool_compacted SSE landing on an
   * earlier message triggers a re-render of just that message.
   * Iterating toolRounds is cheap (avg 2-10 entries). */
  let compactedCount = 0, compactedToSum = 0;
  /* Swarm agent state token: a committed (non-streaming) swarm panel is
   * mutated in place by cross-turn /api/push frames (swarm_push.js). Those
   * mutations live INSIDE round._swarmAgents, which the rest of this
   * fingerprint never sees (it only counts sr.length). Without folding agent
   * status/phase/tokens/preview here, the surgical re-render diffs the
   * swarm-owning message as "unchanged" and the panel never repaints when an
   * agent flips running→done. Piggyback on the existing toolRounds loop. */
  let swarmFp = '';
  /* Tool-round translation token: async auto-translate (stream_lifecycle.js)
   * mutates round._translatedToolContent / _translatedQuestion / the in-flight
   * _toolContentTranslating flag AFTER a message is committed, and the tool
   * renderer (tool_rounds.js) reads them at render time. Like the swarm state
   * above, these live INSIDE toolRounds and were invisible to this fingerprint,
   * so a translation landing on a finalized message was a render no-op (the
   * surgical diff marked the row unchanged) — the reported "translation arrives
   * but UI doesn't refresh" bug. Fold them in on the same cheap loop. */
  let xlFp = '';
  if (Array.isArray(msg.toolRounds)) {
    for (const r of msg.toolRounds) {
      if (r.compactionLayer) compactedCount++;
      compactedToSum += (r.compactedToChars | 0);
      if (r._translatedToolContent) xlFp += 't' + r._translatedToolContent.length;
      if (r._translatedQuestion) xlFp += 'q' + r._translatedQuestion.length;
      if (r._toolContentTranslating) xlFp += 'X';
      if (r._swarm) {
        swarmFp += (r._swarmActive ? 'A' : '') + (r._asyncRunning ? 'R' : '')
                 + (r._swarmEndTime ? 'E' : '') + '|';
        for (const a of (r._swarmAgents || [])) {
          swarmFp += (a.status || '') + ',' + (a.phase || '') + ','
                   + (a.tokens || '') + ',' + ((a.preview || '').length) + ';';
        }
      }
    }
  }
  /* Per-round narration translation token: the interleaved segment timeline
   * renders each round's Chinese from seg.translatedText (stamped by the
   * incremental / whole-message translator). That field is NOT covered by any
   * token above — translatedContent tracks only the DELIVERABLE — so a
   * narration-only translation landing (segmentsByRound / partialByRound) would
   * diff as "unchanged" and never repaint through the surgical renderChat path,
   * leaving the blunt whole-bubble _renderMsgInPlace outerHTML swap (a separate,
   * flicker-prone tick) as the ONLY way tool narration ever turned Chinese.
   * Fold a compact per-round signature (round + translated length) so a
   * narration translation re-renders this row surgically, coalesced with the
   * deliverable's translatedContent change on the same tick — no separate
   * flicker. Cheap: segments avg a handful of text entries. */
  let _segTrFp = '';
  if (Array.isArray(msg.segments)) {
    for (const s of msg.segments) {
      if (!s || s.type !== 'text' || s.deliverable) continue;
      const zh = s.translatedText || '';
      if (!zh) continue;
      _segTrFp += (s.llmRound == null ? '?' : s.llmRound) + '=' + zh.length + ';';
    }
  }
  /* Error envelope: include kind + message length so a re-classification
   * (e.g. quota → ratelimit) bumps the fingerprint and the row re-renders. */
  let _errFp = '';
  if (msg.error) {
    if (typeof msg.error === 'object') {
      _errFp = (msg.error.kind || '') + ':' + ((msg.error.message || '').length);
    } else {
      _errFp = String(String(msg.error).length);
    }
  }
  /* ── Async-provenance fold (RENDER_CONTRACT Invariant 3, L2) ────────────
   * cost + the server-derived modifiedFileList land AFTER a message is
   * committed (the batch prefetches in renderChat / the per-message fallback
   * in finish_info.js). The old fingerprint tracked only `modifiedFiles`
   * (a bare COUNT) and NOTHING for cost, so those data were invisible to the
   * surgical trigger — the reason the separate `_bgRefreshChat` output-diff
   * path exists. Fold them here (cost values + the file-list PATHS, not just
   * their count) so the one surgical trigger repaints when they arrive. */
  let _costFp = '';
  if (msg.cost && typeof msg.cost === 'object') {
    _costFp = (msg.cost.costCny || 0) + '/' + (msg.cost.costUsd || 0);
  }
  let _fcFp = '';
  if (Array.isArray(msg.modifiedFileList) && msg.modifiedFileList.length) {
    _fcFp = _hashStr(msg.modifiedFileList.join('\n'));
  } else if (Array.isArray(msg._undoneFileList) && msg._undoneFileList.length) {
    /* Undone round: the "undone → Redo" bar renders off _undoneFileList, so its
     * presence must move the fingerprint (undo flips modifiedFileList→undone,
     * redo flips it back) or the surgical re-render would miss the state swap. */
    _fcFp = 'u' + _hashStr(msg._undoneFileList.join('\n'));
  } else if (msg._fcResolvedFp) {
    /* Lazy file-change path (RENDER_CONTRACT L2): the extracted fallback list
     * lives in the `_fcResultByMsg` WeakMap (a DIFFERENT shape than the
     * git-backed modifiedFileList, so it must NOT overwrite that authoritative
     * field). finish_info stamps `_fcResolvedFp` = the toolRounds fingerprint
     * it resolved for, so the version MOVES when the extraction lands — the row
     * repaints through the one surgical trigger instead of `_bgRefreshChat`. */
    _fcFp = 'r' + msg._fcResolvedFp;
  }
  let _artFp = '';
  if (Array.isArray(msg._artifacts) && msg._artifacts.length) {
    _artFp = msg._artifacts.length + '@' + _hashStr(
      msg._artifacts.map(a => (a && (a.id || a.name || '')) || '').join(','));
  }
  /* Compaction markers land async (attachCompactionMarkersToConversation on
   * conv open, + the compaction/compaction_done SSE). renderMessage draws an
   * inline card from `msg._compactions[]` (buildCompactionCardHtml) — a field
   * NO other token here covers (the toolRounds compaction fold above tracks a
   * DIFFERENT thing: per-round L1 tool-output shrink). Fold count + each
   * marker's archiveId/status/tokensAfter so a marker arriving or a marker
   * flipping in_progress→done moves the version and the card repaints through
   * the one surgical trigger — the last field `_bgRefreshChat` covered by
   * output-diff that the version did not (RENDER_CONTRACT L2). */
  let _cmFp = '';
  if (Array.isArray(msg._compactions) && msg._compactions.length) {
    _cmFp = msg._compactions.length + '@';
    for (const c of msg._compactions) {
      _cmFp += (c.archiveId || '') + ',' + (c.status || '') + ','
             + (c.tokensAfter || 0) + ';';
    }
  }
  return (msg.role || "") + ":" +
    _hashStr(msg.content || "") + ":" +
    _hashStr(msg.thinking || "") + ":" +
    _errFp + ":" +
    (msg.finishReason || "") + ":" +
    translationFingerprint(msg) + ":" +
    (sr ? sr.length : 0) + ":" +
    (msg._igResult ? "IG" : "") + ":" +
    (msg._igResults ? msg._igResults.length : 0) + ":" +
    (msg._igError ? "IGE" : "") + ":" +
    (msg.modifiedFiles || 0) + ":" +
    (_costFp ? "$" + _costFp : "") + ":" +
    (_fcFp ? "fc" + _fcFp : "") + ":" +
    (_artFp ? "art" + _artFp : "") + ":" +
    (_cmFp ? "cm" + _cmFp : "") + ":" +
    (msg.images ? msg.images.length : 0) + ":" +
    (msg.pdfTexts ? msg.pdfTexts.length : 0) + ":" +
    (msg._autopilotRunId || "") + ":" +
    (msg._isAutopilotSummary ? "S" : "") + ":" +
    /* Queued-pending token: a cross-device queued user message lands in the
     * body with _pendingQueued=true, then dispatch_next_queued reconciles it
     * in place (clearing the flag). Fold it so the flag flip re-renders THIS
     * row surgically — the "queued" chip transitions to a normal sent bubble
     * with no flicker and no whole-conversation repaint. */
    (msg._pendingQueued ? "PQ" : "") + ":" +
    "c" + compactedCount + "ts" + compactedToSum +
    (swarmFp ? ":sw" + swarmFp : "") +
    (_segTrFp ? ":st" + _segTrFp : "") +
    (xlFp ? ":xl" + xlFp : "");
}

/* RENDER_CONTRACT Invariant 3 contract name. `_msgContentVersion(msg)` is the
 * canonical alias for the per-message content version — a pure token that
 * changes iff the rendered state of THIS message would differ. It is exactly
 * `_msgFingerprint` today (during the Phase-2 strangler-fig the two are the
 * same function); callers/tests should reference the contract name so a later
 * increment can retire `_msgFingerprint` without touching them. */
const _msgContentVersion = _msgFingerprint;
if (typeof window !== "undefined") {
  window._msgContentVersion = _msgContentVersion;
  window._msgFingerprint = _msgFingerprint;
  window._hashStr = _hashStr;
}

/* ── Tool-round freshness gradient ──
 *
 * The server compaction cliff (lib/tasks_pkg/compaction.py:MICRO_HOT_TAIL=40)
 * is correct for performance (most runs <40 tool calls) but invisible to
 * the user.  Age-based fading was REMOVED per UX directive: the thing the
 * user actually needs to see — compaction state — is now a SOLID, opaque
 * label per row (see compactionLabel logic in _renderUnifiedToolLine). */

/* ── Shared anchor-relative scroll preservation ──────────────────────────
 * Capture the topmost message element still intersecting the viewport and its
 * offset from the container top; after a DOM mutation that may change heights
 * (including ABOVE the fold), re-pin that element to the same visual offset.
 * Used as a FIXED STEP of both the surgical background repaint (gated on
 * `conv._bgRepaint` — async cost/file-change/compaction data grows bubbles
 * above the fold) and the full-render path (which used to reset scrollTop→0 via
 * innerHTML wipe). Callers wrap the swap in the `cv-off` guard so
 * `content-visibility:auto` intrinsic-size caches can't collapse and
 * mis-resolve the geometry. */
function _captureScrollAnchor(container, inner) {
  if (!container || !inner) return null;
  const cTop = container.getBoundingClientRect().top;
  const els = inner.querySelectorAll('[id^="msg-"]');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.bottom > cTop + 1) {  // topmost element still (partly) in view
      return { id: el.id, offset: r.top - cTop };
    }
  }
  return null;
}
function _restoreScrollAnchor(container, anchor) {
  if (!container || !anchor || !anchor.id) return false;
  const a = document.getElementById(anchor.id);
  if (!a) return false;
  const newOffset = a.getBoundingClientRect().top - container.getBoundingClientRect().top;
  container.scrollTop += (newOffset - anchor.offset);  // re-pin the anchor
  return true;
}

/* ── RENDER_CONTRACT Invariant 3: unified background repaint ──────────────
 * `_bgRefreshChat` USED to be a SECOND, parallel DOM-owner: it re-rendered
 * assistant bubbles by comparing rendered OUTPUT (`__bgHtml`), because the
 * async cost/file-change/compaction data landed in side caches the per-message
 * version could not see. Now that the version HASHES content and folds those
 * fields (cost/modifiedFileList/_fcResolvedFp/_artifacts/_compactions — see
 * _msgFingerprint), the ONE surgical `renderChat(conv,false)` path repaints
 * them, and the anchor-relative scroll preservation `_bgRefreshChat` did is now
 * a fixed step of that path (gated on `conv._bgRepaint`). So this is a thin
 * SHIM onto the single path — there is no longer a separate repaint owner:
 *   • clear the Guard-2 fingerprint (which samples only the LAST message, L6)
 *     so a mid-history data land is not skipped;
 *   • set `_bgRepaint` so the surgical path runs even during _initialSwitchLoad
 *     (the on-open callbacks) AND wraps its swaps in the scroll anchor;
 *   • delegate to renderChat(conv,false).
 * Kept as a named function so its ~7 callers need no edit and the "repaint
 * after async data lands" intent stays greppable. */
function _bgRefreshChat(conv) {
  if (!conv || conv.id !== activeConvId) return;
  if (activeStreams.has(conv.id) && document.getElementById('streaming-msg')) return;
  if (typeof _lastRenderedFingerprint !== 'undefined') _lastRenderedFingerprint = '';
  conv._bgRepaint = true;
  try {
    renderChat(conv, false);
  } finally {
    delete conv._bgRepaint;
  }
}

/* ── RENDER_CONTRACT Invariant 2: id-keyed surgical reconcile ─────────────
 * Locate the existing DOM node for a message by its STABLE `_msgId` (mirrored
 * to `data-msg-id` by renderMessage), falling back to the positional
 * `id="msg-${i}"` only for a legacy message that has no `_msgId` yet.
 *
 * WHY id-first: the surgical diff addresses a message by ARRAY POSITION, but a
 * message's position DRIFTS whenever one is inserted/removed mid-history (an
 * autopilot VU turn, a queued-user row, an edit/branch splice, a lazy-window
 * offset). Position-only matching then can't find the real node for a shifted
 * message and destroys+rebuilds it — discarding that node's live DOM state
 * (an expanded tool `<details>`, the translation-preview zone keyed on
 * `data-msg-id`) on every unrelated edit — and, at the
 * streaming boundary, is the documented root of twin bubbles. Keying on the
 * stable id lets the reconcile REUSE the drifted node (re-stamping its
 * positional `id`/index in place) instead of tearing it down. */
function _reconcileFindEl(inner, msg, i) {
  if (msg && msg._msgId && inner) {
    /* Has a stable id → match by it ONLY. A miss means this message is NEW to
     * the DOM (or its node lives elsewhere); we must NOT fall back to the
     * positional `msg-${i}` handle, because after an index shift that slot
     * belongs to a DIFFERENT message — grabbing it would reuse the wrong node
     * (and strand the real one). The caller treats null as "insert". */
    if (typeof CSS !== 'undefined' && CSS.escape) {
      return inner.querySelector('[data-msg-id="' + CSS.escape(msg._msgId) + '"]');
    }
    return inner.querySelector('[data-msg-id="' + msg._msgId + '"]');
  }
  /* Legacy id-less message: the positional handle is the only key available. */
  return document.getElementById('msg-' + i);
}

/* ── RENDER_CONTRACT Invariant 3 / L10: resolve a message's LIVE index ────
 * The per-message action buttons (Delete/Edit/Regen/Translate/Copy/Branch/…)
 * and the thinking/pdf/ig affordances used to bake the array index straight
 * into their `onclick` (`deleteTurn(3)`). But the id-keyed reconcile REUSES a
 * drifted-but-unchanged node without rebuilding its HTML, so after a
 * mid-history insert/splice a bubble that moved 3→4 still ran `deleteTurn(3)`
 * — the action hit the WRONG turn. It also made `renderMessage` output
 * index-DEPENDENT, which blocks a rendered-output content-version.
 *
 * `_msgElIndex(el)` resolves the CURRENT array index at CLICK time from the
 * nearest `.message` node's stable `data-msg-id` against the active
 * conversation (positional `id="msg-N"` legacy fallback for an id-less node).
 * Every action onclick calls `fn(_msgElIndex(this))`, so the rendered string
 * no longer encodes a position and stays correct across drift. Unknown → -1,
 * which every handler already treats as a no-op (`conv.messages[-1]` is
 * undefined → the `if (!msg) return` guard). */
function _msgElIndex(el) {
  if (!el || typeof el.closest !== 'function') return -1;
  const node = el.closest('.message');
  if (!node) return -1;
  const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
  const mid = node.getAttribute && node.getAttribute('data-msg-id');
  if (mid && conv && Array.isArray(conv.messages)) {
    for (let i = 0; i < conv.messages.length; i++) {
      if (conv.messages[i] && conv.messages[i]._msgId === mid) return i;
    }
    return -1;  // stable id present but no longer in the list → no-op
  }
  /* Legacy id-less node: fall back to the positional `id="msg-N"` handle. */
  const m = node.id && node.id.match(/^msg-(\d+)$/);
  return m ? parseInt(m[1], 10) : -1;
}
if (typeof window !== "undefined") {
  window._msgElIndex = _msgElIndex;
}

/* ── First-open skeleton (epic ②) ──────────────────────────────────────
 * Paint an INSTANT placeholder into #chatInner for a `_needsLoad` conversation
 * — before loadConversationMessages' cache/server fetch returns — so opening an
 * unopened conv shows its title + N shimmer bubbles immediately instead of a
 * blank screen (or the previous conv frozen under the reader) during the up-to
 * 15s server round-trip on a flaky tunnel.
 *
 * DOM ALIGNMENT (zero-CLS swap): the skeleton reuses the EXACT real message
 * structure renderMessage() emits — `#chatInner` > `.message[.user-msg]` >
 * `.message-avatar` + `.message-content`(`.message-header`>`.message-role`,
 * `.message-body`) — so when the real render replaces innerHTML the layout /
 * scroll anchor don't jump. Bubbles are keyed with `id="skeleton-msg-N"` (never
 * `msg-*`) so renderChat's surgical `_hasMsgDom` probe never mistakes them for
 * real messages and the first real render always takes the full-wipe path.
 *
 * Count: clamp `msgCount` to a small visual cap (real turns render over it in
 * milliseconds; the cap just bounds the DOM we throw away). Alternates
 * user/assistant so it reads like a conversation. Caller guarantees msgCount>0
 * (an empty conv gets the welcome/empty state, never a skeleton).
 */
function renderSkeletonChat(conv, msgCount) {
  const inner = document.getElementById('chatInner');
  if (!inner) return false;
  const n = Math.max(1, Math.min(msgCount || 1, 6));
  const title = (conv && conv.title) || 'Untitled';
  const _skT = (k, fb) => (typeof t === 'function' && t(k) !== k) ? t(k) : fb;
  const _roleLabel = _skT('role.assistant', 'Assistant');
  let html = '';
  for (let i = 0; i < n; i++) {
    /* Newest turn is an assistant reply, so count backwards: even distance from
     * the end = assistant, odd = user (matches a typical user→assistant tail). */
    const isUser = ((n - 1 - i) % 2) === 1;
    const lines = isUser ? 1 : (2 + (i % 3));   // assistant bubbles look longer
    let body = '';
    for (let l = 0; l < lines; l++) {
      const w = isUser ? (55 + (i * 7) % 30) : (72 + (l * 9) % 24);
      body += `<div class="chat-sk-line" style="width:${w}%"></div>`;
    }
    html += `<div class="message chat-skeleton${isUser ? ' user-msg' : ''}" id="skeleton-msg-${i}" aria-hidden="true">`
      + `<div class="message-avatar"><div class="chat-sk-avatar"></div></div>`
      + `<div class="message-content"><div class="message-header">`
      + `<span class="message-role">${isUser ? _skT('role.you', 'You') : _roleLabel}</span></div>`
      + `<div class="message-body">${body}</div></div></div>`;
  }
  /* Bubbles are DIRECT children of #chatInner (mirrors the real full render at
   * inner.innerHTML=html) so the swap to real messages is zero-CLS. The wrapping
   * `.messages` box is intentionally NOT used (the real render doesn't emit one).
   * `title` is captured for a11y intent but not rendered as a layout box. */
  void title;
  inner.innerHTML = html;
  return true;
}

function renderChat(conv, forceScroll) {
  /* ── Guard 1: skip if user is editing a message in this conversation ── */
  if (_editingMsgIdx !== null && conv.id === activeConvId) return;
  /* Prefetch all per-message costs in ONE batch round-trip so the
   * synchronous calcCostCny() calls inside renderFinishInfo() hit the
   * cache.  Fire-and-forget; if fresh entries landed, force a re-render
   * (forceScroll=true bypasses the fingerprint guard).  When everything
   * was already cached, the prefetch returns false and we don't paint. */
  if (typeof _prefetchConvCosts === 'function') {
    _prefetchConvCosts(conv).then((didFetch) => {
      // ★ Skip the re-render while this conv is actively streaming. The
      //   streaming bubble's cost/finish bar is owned by the live stream UI,
      //   not this static render — see the file-changes loop fix below.
      // ★ SCROLL FIX: repaint IN PLACE (scroll-preserving) instead of
      //   renderChat(conv,true). The old force-scroll path reset the lazy
      //   window and yanked a scrolled-up reader to the bottom the moment the
      //   cost batch landed (~1s after open) — the "flicker + jump" bug.
      if (didFetch && conv.id === activeConvId
          && !activeStreams.has(conv.id)) {
        _bgRefreshChat(conv);
      }
    });
  }
  /* Same idea for file-changes-bar derivation. Without this, every
   * message lacking a server-derived modifiedFileList fired its own
   * single POST /api/v1/messages/extract-file-changes from inside
   * renderFileChangesBar. With ~10 such messages this was the second-
   * biggest source of post-migration render lag (after cost). */
  if (typeof _prefetchConvFileChanges === 'function') {
    _prefetchConvFileChanges(conv).then((didFetch) => {
      // ★ FIX (continuous fc-bar flash + scroll-jam): while the conv is
      //   streaming, the in-progress message's toolRounds keep growing, so the
      //   coarse _fcFingerprint keeps changing → the prefetch keeps returning
      //   didFetch=true → renderChat(conv,true) → Guard 1c →
      //   showStreamingUIForConv → _forceScrollToBottom, in a tight loop that
      //   replays the fcPulse/fcFileIn animations and pins the view to the
      //   bottom. The streaming bubble's file-changes bar is rendered by the
      //   live `fc` zone (updateStreamingUI), so this static re-render is both
      //   redundant and harmful mid-stream. Defer it until the stream ends.
      // ★ SCROLL FIX: scroll-preserving in-place repaint (see _bgRefreshChat)
      //   instead of the force-scroll full re-render — the file-changes batch
      //   also lands ~1s after open and must not move a scrolled-up reader.
      if (didFetch && conv.id === activeConvId
          && !activeStreams.has(conv.id)) {
        _bgRefreshChat(conv);
      }
    });
  }

  /* ── Guard 1b: skip background re-renders while branch panel is open ── */
  if (forceScroll === false && _activeBranch && conv.id === activeConvId) return;

  /* ── Guard 1c: protect active streaming bubble from destruction ──
   * A full renderChat() destroys the #streaming-msg element and replaces all
   * messages with static renderMessage() output.  The streaming assistant message
   * (which has msg.model set from the state/preset SSE event) gets a finish-bar
   * with only the model tag — appearing as if the message is done while the
   * sidebar still pulses and the stop button is active.
   * Fix: delegate to showStreamingUIForConv() which properly renders prev messages
   * statically and creates a fresh streaming bubble for the in-progress message. */
  if (conv.id === activeConvId && activeStreams.has(conv.id) && document.getElementById('streaming-msg')) {
    if (typeof showStreamingUIForConv === 'function') showStreamingUIForConv(conv.id);
    return;
  }

  /* ── Guard 2: fingerprint-based skip for background syncs (forceScroll===false) ── */
  const fp = _convRenderFingerprint(conv);
  if (
    forceScroll === false &&
    conv.id === activeConvId &&
    fp === _lastRenderedFingerprint
  ) {
    /* Data hasn't actually changed — skip the destructive re-render entirely */
    return;
  }

  const inner = document.getElementById("chatInner");
  const container = document.getElementById("chatContainer");

  /* ═══ Surgical update path (forceScroll === false) ═══
   * Instead of wiping inner.innerHTML (which destroys all DOM nodes,
   * resets content-visibility:auto size caches, and causes scroll flicker),
   * do per-message diffing: only touch messages that actually changed.
   * This preserves scroll position perfectly with ZERO visual flicker.
   *
   * Only use surgical mode when the DOM already has rendered messages
   * (i.e. not showing a welcome screen or loading skeleton). */
  const _hasMsgDom = inner && inner.querySelector('[id^="msg-"]');
  /* ★ FIX: During initial conversation load (_initialSwitchLoad), Phase 2 server
   * response triggers renderChat(conv, false).  The surgical path would do
   * outerHTML replacement which destroys content-visibility:auto size caches,
   * causing scrollHeight to collapse → visible scroll-jump-to-top before the
   * .then() callback scrolls back down.  Skip surgical mode for initial loads
   * so the full-render path runs with _forceScrollToBottom — no flash. */
  /* ★ A BACKGROUND REPAINT (`conv._bgRepaint`, set by the _bgRefreshChat shim)
   *   takes the surgical path EVEN during `_initialSwitchLoad`: the on-open
   *   compaction/artifact/cost/file-change callbacks land async data while the
   *   initial switch is still in flight, and they must repaint in place
   *   (scroll-preserved, see the anchor wrap below), NOT force-scroll. The
   *   `_initialSwitchLoad` bail below still applies to a NON-background render
   *   during the open (the first paint), which correctly uses full-render. */
  const _bgRepaint = !!conv._bgRepaint;
  if (forceScroll === false && conv.id === activeConvId && conv.messages.length > 0 && _hasMsgDom && (!conv._initialSwitchLoad || _bgRepaint)) {
    const total = conv.messages.length;
    /* ★ FIX: Respect _lazyRenderedFrom so force-loaded messages (from scrollToTurn
     * or manual scroll-up) survive surgical updates.  Previously this always used
     * total - _INITIAL_RENDER, which removed force-loaded messages and left
     * _lazyRenderedFrom stale — making turn-nav dots unclickable a second time. */
    const defaultStart = Math.max(0, total - _INITIAL_RENDER);
    const startIdx = (_lazyConvId === conv.id && _lazyRenderedFrom < defaultStart)
      ? _lazyRenderedFrom
      : defaultStart;
    let anyChange = false;
    /* ★ RENDER_CONTRACT Invariant 3: anchor-relative scroll preservation is now
     *   a FIXED STEP of the surgical path (owner directive), absorbed from the
     *   retired `_bgRefreshChat`. Async data (cost/file-change/compaction) lands
     *   above the fold and GROWS bubbles; without re-pinning, a scrolled-up
     *   reader drifts. Capture the top in-view anchor before the swaps under the
     *   `cv-off` guard (so content-visibility:auto caches can't collapse and
     *   mis-resolve geometry) and re-pin after. */
    const _bgContainer = document.getElementById('chatContainer');
    let _bgAnchor = null;
    if (_bgRepaint && _bgContainer && inner) {
      inner.classList.add('cv-off');
      void inner.scrollHeight;
      _bgAnchor = _captureScrollAnchor(_bgContainer, inner);
    }

    /* 1) Update or add messages
     * ★ Perf: collect all outerHTML replacements first, then apply in one pass.
     * Each outerHTML assignment invalidates layout; batching avoids interleaving
     * layout reads (getElementById) with writes (outerHTML) — prevents forced reflows. */
    const _pendingUpdates = [];
    /* ★ Skip the streaming message — it's rendered as #streaming-msg,
     *   not as msg-N.  Without this skip, the else branch below would
     *   append a static renderMessage() (with finish-bar) for the streaming
     *   message, then step 3 would remove the live streaming bubble. */
    const _streamingActive = activeStreams.has(conv.id) && document.getElementById('streaming-msg');
    const _skipIdx = _streamingActive ? (total - 1) : -1;
    /* ★ BOUNDED WINDOW: when a downward eviction has capped the tail
     * (_lazyRenderedTo < total, set by _loadOlderMessages while the reader is
     * up in history), the surgical diff must NOT re-append the evicted tail —
     * that would silently rebuild the unbounded DOM this optimisation removes.
     * The bottom sentinel + _loadNewerMessages own re-rendering the tail on
     * scroll-down. Uncapped (Infinity, the common case incl. all streaming) →
     * this equals `total`, byte-identical to before. */
    const _surgTo = (_lazyConvId === conv.id && Number.isFinite(_lazyRenderedTo))
      ? Math.min(total, _lazyRenderedTo) : total;
    /* ★ Id-keyed reconcile (RENDER_CONTRACT Invariant 2). `_cursor` tracks the
     * DOM position the NEXT reconciled node must occupy, so a reused-but-drifted
     * node is MOVED into array order instead of left stranded. `_reconcileFindEl`
     * matches by stable `_msgId` first (positional `id` only as a legacy
     * fallback), so a shifted-unchanged node is REUSED — its live DOM state
     * (expanded tool <details>, translation preview) survives — and
     * only its positional `id`/index handle is re-stamped. */
    let _cursor = _skipIdx >= 0 ? document.getElementById('msg-' + _skipIdx) : null;
    /* Anchor for insertion: the node the reconciled sequence must sit BEFORE.
     * We walk forward, placing each node right after the previous one. */
    let _prevEl = null;
    for (let i = startIdx; i < _surgTo; i++) {
      if (i === _skipIdx) continue;  // streaming message — leave #streaming-msg alone
      const msg = conv.messages[i];
      const el = _reconcileFindEl(inner, msg, i);
      if (el) {
        /* Element exists (matched by _msgId or legacy index) — check if content
         * changed. A drifted node keeps its live DOM state; we only re-stamp its
         * positional id and re-order it. */
        const oldFp = el.getAttribute("data-mfp") || "";
        const newFp = _msgFingerprint(msg);
        if (oldFp !== newFp) {
          _pendingUpdates.push({ el, html: renderMessage(msg, i) });
          anyChange = true;
        } else if (el.id !== ("msg-" + i)) {
          /* Reused drifted node whose content is unchanged: re-stamp only the
           * positional handle so index-addressing consumers (edit_message.js,
           * streaming_render.js) stay correct without a destroy+rebuild. */
          el.id = "msg-" + i;
          anyChange = true;
        }
        /* Re-position into array order if it drifted out of place. */
        const _want = _prevEl ? _prevEl.nextSibling : (_cursor ? _cursor.nextSibling : inner.firstChild);
        if (el !== _want && el.parentNode === inner) {
          inner.insertBefore(el, _want);
          anyChange = true;
        } else if (el.parentNode !== inner) {
          inner.insertBefore(el, _prevEl ? _prevEl.nextSibling : null);
          anyChange = true;
        }
        _prevEl = el;
      } else {
        /* New message — insert at the cursor position (array order), not blind append. */
        const wrapper = document.createElement("div");
        wrapper.innerHTML = renderMessage(msg, i);
        const newEl = wrapper.firstElementChild;
        if (newEl) {
          inner.insertBefore(newEl, _prevEl ? _prevEl.nextSibling : (_cursor ? _cursor.nextSibling : null));
          _prevEl = newEl;
        }
        anyChange = true;
      }
    }
    /* Apply all outerHTML replacements in a single write batch. Re-resolve each
     * node's live reference AFTER the reorder pass (outerHTML needs the current
     * DOM node), matching by the stamped id we just set. */
    for (const upd of _pendingUpdates) {
      if (upd.el && upd.el.parentNode) upd.el.outerHTML = upd.html;
    }

    /* 2) Remove stale messages beyond the current count
     * ★ FIX: Use startIdx (which respects _lazyRenderedFrom) instead of
     * recalculating total - _INITIAL_RENDER — keeps force-loaded messages alive. */
    const staleEls = inner.querySelectorAll('[id^="msg-"]');
    for (const el of staleEls) {
      const m = el.id.match(/^msg-(\d+)$/);
      if (m) {
        const idx = parseInt(m[1], 10);
        if (idx >= total || idx < startIdx) {
          el.remove();
          anyChange = true;
        }
      }
    }

    /* 3) Remove any leftover streaming bubble (task finished)
     * ★ Only remove if the stream has actually finished — don't destroy a live
     *   streaming bubble.  Guard 1c should have caught this, but belt-and-suspenders. */
    const leftoverStreaming = document.getElementById("streaming-msg");
    if (leftoverStreaming && !activeStreams.has(conv.id)) {
      leftoverStreaming.remove();
      anyChange = true;
    }

    if (anyChange) {
      buildTurnNav(conv);
    }
    if (!activeStreams.has(conv.id)) {
      _applyAutopilotRunFolds(inner, conv);
    }
    /* ★ Re-pin the anchor captured above (background repaint only) so an
     *   above-fold height change from the freshly-landed data doesn't drift a
     *   scrolled-up reader, then drop the cv-off guard. */
    if (_bgRepaint && _bgContainer) {
      void inner.scrollHeight;
      if (_bgAnchor) _restoreScrollAnchor(_bgContainer, _bgAnchor);
      inner.classList.remove('cv-off');
    }
    _lastRenderedFingerprint = fp;
    _lazyConvId = conv.id;
    return;
  }

  /* ═══ Full re-render path (forceScroll !== false) ═══ */
  /* ★ Jump-to-top fix: the innerHTML wipe below hard-resets scrollTop→0. When
   *   this is a SAME-conversation re-render and the reader had scrolled UP to
   *   read history, capture their viewport anchor from the still-present old
   *   DOM so we can re-pin it after the swap instead of yanking to the bottom
   *   via _forceScrollToBottom. A genuine conversation SWITCH, a first load, or
   *   a near-bottom reader still lands at the bottom (the expected behaviour for
   *   opening a conversation / following a live reply). */
  const _sameConvDom = _lazyConvId === conv.id
    && inner && !!inner.querySelector('[id^="msg-"]');
  const _readerNearBottom = (typeof isNearBottom === 'function')
    ? isNearBottom(120) : true;
  /* ★ Open-scroll coalescing. A single conversation OPEN drives SEVERAL full
   *   renders in sequence — the Phase-1 IndexedDB paint, the Phase-2 server
   *   reconcile paint, and the loadConversation() .then() fallback — each of
   *   which used to _forceScrollToBottom. That is why opening a historical
   *   conversation "jumped several times". We now position the view EXACTLY
   *   ONCE per open: the first full render during the open (conv._initialSwitchLoad
   *   set) scrolls to the bottom and latches _openScrollConvId; every LATER
   *   render of that same open preserves the current viewport via the anchor
   *   instead of re-snapping. (A genuine SWITCH first-paint has _lazyConvId
   *   still pointing at the previous conv, so _sameConvDom is false and it
   *   correctly force-scrolls + latches.) */
  const _openAlreadyPositioned = !!conv._initialSwitchLoad
    && _openScrollConvId === conv.id;
  const _preSwapAnchor = (_sameConvDom && !activeStreams.has(conv.id)
      && (!_readerNearBottom || _openAlreadyPositioned))
    ? _captureScrollAnchor(container, inner)
    : null;
  _destroyLazyObserver();
  _lazyConvId = conv.id;

  if (conv.messages.length === 0) {
    if (conv._needsLoad) {
      /* ── Loading skeleton: conv has server messages but they haven't arrived yet ── */
      inner.innerHTML = String(safeHtml`<div class="welcome" id="welcome" style="opacity:0.5"><div class="welcome-icon" style="animation:pulse 1.5s infinite"></div><h2>Loading conversation…</h2><p>Fetching ${conv._serverMsgCount || ''} messages from server</p></div>`);
    } else {
      /* BASE_PATH is a trusted app constant (raw); the i18n subtitle is
       * escaped by default. */
      inner.innerHTML = String(safeHtml`<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${raw(BASE_PATH)}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2><div class="feature-pills">${raw(_welcomePillsHtml())}</div></div>`);
    }
    _lastRenderedFingerprint = fp;
    buildTurnNav(conv);
    return;
  }

  const total = conv.messages.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;
  /* Full render paints the TAIL [startIdx, total) — the bottom is uncapped, so
   * reset the upper window bound. A later upward lazy-load seeds a finite cap
   * and installs the bottom sentinel. */
  _lazyRenderedTo = total;

  let html = "";

  /* Lazy-load sentinel for older messages */
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }

  /* Render only the tail portion */
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(conv.messages[i], i);
  }

  inner.innerHTML = html;
  if (!activeStreams.has(conv.id)) {
    _applyAutopilotRunFolds(inner, conv);
  }
  _lastRenderedFingerprint = fp;

  /* Observe the sentinel to trigger loading when scrolled up */
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }

  /* ★ PERF: Defer buildTurnNav to after paint — it scans ALL messages and
   * JSON.parse-s every tool round's args, which can take 50-200ms for large
   * conversations.  The turn nav is not critical for the initial render. */
  requestAnimationFrame(() => buildTurnNav(conv));

  /* Scroll handling: if we captured a pre-swap anchor (same-conv re-render, a
   * reader parked up in history), re-pin their viewport instead of forcing to
   * the bottom — this is the direct fix for the "sudden jump to the top/bottom"
   * on a background full re-render. Otherwise (conversation switch, first load,
   * near-bottom reader) land at the bottom as before.
   * hideUntilSettled=true: content-visibility:auto heights are estimated on
   * first paint, so hide until the 150ms timer has corrected scrollTop. */
  if (_preSwapAnchor) {
    inner.classList.add('cv-off');
    void inner.scrollHeight;  // force real heights before re-pinning
    _restoreScrollAnchor(container, _preSwapAnchor);
    inner.classList.remove('cv-off');
  } else if (!_sameConvDom || conv._initialSwitchLoad) {
    /* ★ No-auto-scroll-on-OPEN (owner directive). A conversation OPEN — a
     *   genuine switch (`!_sameConvDom`: the DOM currently shows a different
     *   conv / welcome), a first load, or any render during the initial switch
     *   load (`_initialSwitchLoad`) — must NOT yank the view to the bottom.
     *   The `inner.innerHTML` wipe above already reset scrollTop→0 (the top of
     *   the rendered tail window); we simply leave it there so opening a
     *   conversation keeps a stable position instead of jumping. Still latch
     *   this open so LATER same-open renders (Phase-2 reconcile, the
     *   loadConversation .then fallback) take the anchor-preserve branch above
     *   and HOLD this position rather than re-snapping. (The streaming-follow
     *   path — showStreamingUIForConv / the send pipeline — is deliberately
     *   untouched and still lands at the bottom.) */
    if (conv._initialSwitchLoad) _openScrollConvId = conv.id;
  } else {
    /* Same-conv background full re-render with a near-bottom reader (a settle /
     *   poll following new content — NOT a conversation open): keep them pinned
     *   to the bottom so the newest content stays in view. */
    _forceScrollToBottom(container, true);
  }
}

/* ★ Format precise date + time for finished messages */
function _fmtAbsoluteDateTime(ts) {
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
/* Is a LIVE stream bound to THIS message object? Identity-based (never array
 * position): an activeStreams entry whose bound `assistantMsg` IS this object,
 * or whose `taskId` matches this message's stamped `_taskId`. Mirrors how
 * connectToTask resolves its accumulation slot by identity (_taskId), so a
 * genuinely-streaming "Preparing…" pre-first-token bubble is recognised. */
function _streamBoundToMsg(msg) {
  if (!msg || typeof activeStreams === "undefined" || !activeStreams.size) return false;
  for (const s of activeStreams.values()) {
    if (!s) continue;
    if (s.assistantMsg && s.assistantMsg === msg) return true;
    if (s.taskId && msg._taskId && s.taskId === msg._taskId) return true;
  }
  return false;
}

/* #3 render-time BELT (last line of defense behind #1 warm-open adoption and
 * #2 settle-time reconcile): an assistant bubble that is an ORPHAN placeholder
 * must not render. Orphan = no user-visible payload of ANY kind AND no live
 * stream bound. Deliberately NOT keyed on merely-empty content, or a legitimate
 * pre-first-token "Preparing…" bubble (empty but stream-bound) would blank. */
function _isOrphanEmptyAssistant(msg) {
  if (!msg || msg.role !== "assistant") return false;
  if (_streamBoundToMsg(msg)) return false;
  if (msg.content && String(msg.content).length) return false;
  if (msg.thinking && String(msg.thinking).length) return false;
  if (msg.toolRounds && msg.toolRounds.length) return false;
  if (msg.finishReason) return false;
  if (msg.error) return false;
  return true;
}

/* ── Backend-faithful ghost-tail predicates (empty-bubble root fix ②/③) ──
 * These are exact JS ports of lib/conversations/reconcile.py so the frontend
 * can settle an empty turn IN-SESSION (done handler + finishStream) with the
 * SAME verdict the backend GET/startup reconcile applies — instead of leaving
 * a finishReason-stamped empty shell that only heals on the next warm reopen.
 * Byte-equivalence with the Python is pinned by
 * tests/test_reconcile_js_backend_equivalence.py.
 *
 * `_hasRealToolRound` ≡ reconcile._has_real_round: at least one settled /
 * result-bearing tool round (status==='done', a toolContent blob, or a
 * non-empty results array). */
function _hasRealToolRound(msg) {
  const rounds = msg && msg.toolRounds;
  if (!Array.isArray(rounds)) return false;
  for (const r of rounds) {
    if (!r || typeof r !== "object") continue;
    if (r.status === "done" || r.toolContent) return true;
    if (Array.isArray(r.results) && r.results.length) return true;
  }
  return false;
}

/* `_classifyGhostTailJS` ≡ reconcile.classify_ghost_tail (a FAITHFUL 1:1 port —
 * byte-equivalence pinned by tests/test_frontend_ghost_tail_js_backend_
 * equivalence.py): verdict for a TRAILING assistant. Returns 'delete' (bare
 * empty husk), 'interrupt' (thinking-only husk — preserve reasoning) or null
 * (settled / keep). A ghost tail is an assistant with no settled output: empty
 * content, no finishReason/usage/error, no real tool round.
 *
 * NOTE: like the backend, this does NOT special-case endpoint/VU turns — the
 * `role !== 'assistant'` gate already excludes VU (role='user') and critic
 * (role='user'/'optimizer'); an empty endpoint-planner ASSISTANT tail is a
 * genuine ghost the backend deletes too. Any special-turn protection a CALLER
 * needs (e.g. the live done-handler's detached-carrier / VU-takeover cases)
 * lives at that call site, not in this authority-matching predicate. */
function _classifyGhostTailJS(msg) {
  if (!msg || msg.role !== "assistant") return null;
  if ((msg.content && String(msg.content).trim())
      || msg.finishReason || msg.usage || msg.error) return null;
  if (_hasRealToolRound(msg)) return null;
  return (msg.thinking && String(msg.thinking).trim()) ? "interrupt" : "delete";
}
if (typeof window !== "undefined") {
  window._hasRealToolRound = _hasRealToolRound;
  window._classifyGhostTailJS = _classifyGhostTailJS;
}

/* Build the manual /compact boundary card (docs/MANUAL_COMPACTION_DESIGN.md
 * §5.3). A PURE render of the backend-stamped compaction marker fact — no
 * client-side lifecycle inference. Default COLLAPSED; the head toggles the
 * body; "查看压缩前快照" opens the existing viewer. All stats come from
 * `msg._compactions[0]` (the backend stamps tokensBefore/After + msgs +
 * reductionPct there); the summary text is `msg.content` minus its header
 * line. Extracted as a standalone fn so it can be unit-tested without driving
 * the whole renderMessage dependency graph. */
function buildCompactionCardHtml(msg) {
  const _cm = (Array.isArray(msg._compactions) && msg._compactions[0]) || {};
  const _archiveId = (msg._compactionArchiveId != null)
    ? msg._compactionArchiveId : _cm.archiveId;
  const _convId = _cm.convId || (typeof activeConvId !== 'undefined' ? activeConvId : '');
  const _tb = _cm.tokensBefore || 0, _ta = _cm.tokensAfter || 0;
  const _mb = _cm.msgsBefore || 0, _ma = _cm.msgsAfter || 0;
  const _rp = (_cm.reductionPct != null) ? _cm.reductionPct
            : (_tb > 0 ? Math.round((1 - _ta / _tb) * 100) : 0);
  const _fmtK = (n) => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  const _tokStat = (_tb > 0 && _ta > 0)
    ? `${_fmtK(_tb)} → ${_fmtK(_ta)} tokens · -${_rp}%` : '';
  // 档B (intra-turn fold): the card describes "folded N tool rounds" rather
  // than "N → M msgs" (a single-giant-turn compaction removes tool rounds
  // inside one message, not whole messages). Backend stamps the count on the
  // marker (`foldedToolRounds`) / message (`_foldedToolRounds`).
  const _folded = (_cm.foldedToolRounds || msg._foldedToolRounds || 0);
  const _msgStat = _folded > 0
    ? (typeof t === 'function' ? t('compactCard.rounds', { n: _folded })
                               : `folded ${_folded} tool rounds`)
    : ((_mb > 0 && _ma > 0)
        ? (typeof t === 'function' ? t('compactCard.msgs', { before: _mb, after: _ma })
                                   : `${_mb} → ${_ma} msgs`) : '');
  // Strip the leading header line ("## 上下文已压缩…") — the card has its own title.
  const _rawSummary = String(msg.content || '').replace(/^##[^\n]*\n+/, '');
  const _summaryHtml = renderMarkdown(_rawSummary);
  const _title = (typeof t === 'function') ? t('compactCard.title') : '上下文已压缩';
  const _expandLabel = (typeof t === 'function') ? t('compactCard.expand') : '展开摘要';
  const _viewLabel = (typeof t === 'function') ? t('compactCard.viewSnapshot') : '查看压缩前快照';
  const _scissorsSvg = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/></svg>';
  const _viewBtn = (_archiveId != null)
    ? `<button type="button" class="compact-card-view"
         data-conv-id="${escapeHtml(_convId)}" data-archive-id="${_archiveId}"
         onclick="if(window.openCompactionViewer){window.openCompactionViewer(this.dataset.convId, parseInt(this.dataset.archiveId,10))}">${escapeHtml(_viewLabel)}</button>`
    : '';
  return `<div class="compact-card" data-collapsed="1">
    <button type="button" class="compact-card-head" onclick="this.parentElement.dataset.collapsed = this.parentElement.dataset.collapsed==='1'?'0':'1'">
      <span class="compact-card-icon">${_scissorsSvg}</span>
      <span class="compact-card-title">${escapeHtml(_title)}</span>
      ${_tokStat ? `<span class="compact-card-stat">${escapeHtml(_tokStat)}</span>` : ''}
      ${_msgStat ? `<span class="compact-card-stat compact-card-stat-msgs">${escapeHtml(_msgStat)}</span>` : ''}
      <span class="compact-card-toggle" aria-hidden="true">${escapeHtml(_expandLabel)}</span>
    </button>
    <div class="compact-card-body"><div class="md-content">${_summaryHtml}</div>${_viewBtn}</div>
  </div>`;
}

function renderMessage(msg, idx) {
  /* Belt: drop an orphaned empty-assistant placeholder (see
   * _isOrphanEmptyAssistant). Returning '' produces no DOM node — the surgical
   * update path replaces via outerHTML='' (removes it), the append path skips a
   * null firstElementChild, and the full render contributes nothing. */
  if (_isOrphanEmptyAssistant(msg)) return "";
  const isUser = msg.role === "user" || msg.role === "optimizer";  // optimizer = endpoint review, render as user
  /* Translation display state via the canonical model (core/translation_model.js):
   *   _disp — resolves WHICH content string + how to render it, with NO
   *           translation logic (this is what erases the old inversion branch).
   *   _tr   — the translation status/text/model object.
   *   _showTrans — show the 译文 instead of the content: a display-translated
   *           message (assistant / critic / VU, i.e. _disp.isMarkdown) that has
   *           a translation and is not toggled off. Feeds the body-selection
   *           below AND the assistant/critic bilingual blocks. */
  const _disp = displayContent(msg);
  const _tr = readTranslation(msg);
  const _showTrans = !!_disp.isMarkdown && !!_tr.text && _tr.showing !== false;
  /* ★ Precise date + time for all messages */
  const messageTime = msg.timestamp ? _fmtAbsoluteDateTime(msg.timestamp) : "";
  let body = "";
  if (msg.images?.length > 0) {
    const srcMap = { clip_render: "CLIP", vector_clip: "VEC", page_render: "SCAN", embedded: "RAW", pixmap_fallback: "PIX", pymupdf4llm: "FIG", figure_page_render: "FIG" };
    body += '<div class="msg-image-grid">';
    body += msg.images.map((img) => {
      const src = img.preview || "";
      const isPdf = !!img.pdfPage;
      const srcLabel = srcMap[img.pdfImageSource] || (isPdf ? "PDF" : "");
      const label = isPdf
        ? `P${img.pdfPage}/${img.pdfTotal} · ${img.sizeKB}KB`
        : `${img.sizeKB || "?"}KB`;
      const tip = img.caption
        ? escapeHtml(String(img.caption))
        : isPdf ? `PDF page ${img.pdfPage}` : "";
      if (src && !src.endsWith("..."))
        return `<div class="msg-img-thumb${isPdf ? " pdf-page" : ""}" ${tip ? `title="${tip}"` : ""} onclick="openImagePreview('${src.replace(/'/g, "\\'")}')"><img src="${src}" alt="uploaded">${srcLabel ? `<div class="msg-img-badge">${srcLabel}</div>` : ""}<div class="msg-img-size">${label}</div></div>`;
      return `<div class="msg-img-thumb placeholder"><span class="msg-img-placeholder-icon"></span><div class="msg-img-size">${img.sizeKB || "?"}KB</div></div>`;
    }).join("");
    body += '</div>';
  }
  if (isUser && msg.pdfTexts?.length > 0) {
    body += '<div class="pdf-attachments-indicator">';
    msg.pdfTexts.forEach((pdf, pdfI) => {
      const sizeStr =
        pdf.textLength >= 1024
          ? `${(pdf.textLength / 1024).toFixed(1)}KB`
          : `${pdf.textLength} chars`;
      const scanBadge = pdf.isScanned ? " · scanned" : "";
      const imgCount = (msg.images || []).filter(
        (img) => img.pdfName === pdf.name,
      ).length;
      const imgStr =
        imgCount > 0 ? ` · ${imgCount} img${imgCount > 1 ? "s" : ""}` : "";
      const methodBadge = pdf.method === "vlm" ? ' · <b>VLM</b>' : '';
      const _ext = pdf.name ? pdf.name.slice(pdf.name.lastIndexOf('.')).toLowerCase() : '';
      const _dsvg = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
      const _DOC_FILE = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>';
      const _DOC_SHEET = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M8 13h2"/><path d="M14 13h2"/><path d="M8 17h2"/><path d="M14 17h2"/>';
      const _DOC_CODE = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 12.5 8 15l2 2.5"/><path d="m14 12.5 2 2.5-2 2.5"/>';
      const _DOC_SLIDE = '<path d="M2 3h20"/><path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3"/><path d="m7 21 5-5 5 5"/>';
      const _docIconMap = {'.pdf':_DOC_FILE, '.docx':_DOC_FILE, '.pptx':_DOC_SLIDE, '.xlsx':_DOC_SHEET, '.txt':_DOC_FILE, '.md':_DOC_FILE,
                           '.csv':_DOC_SHEET, '.json':_DOC_CODE, '.xml':_DOC_CODE, '.py':_DOC_CODE, '.js':_DOC_CODE,
                           '.html':_DOC_CODE, '.yaml':_DOC_CODE, '.yml':_DOC_CODE};
      const docIcon = _dsvg(_docIconMap[_ext] || _DOC_FILE);
      body += `<div class="pdf-attach-badge" title="${escapeHtml(pdf.name)}" onclick="previewMsgPdfText(_msgElIndex(this),${pdfI})" style="cursor:pointer"><span class="pdf-attach-icon">${docIcon}</span><span class="pdf-attach-info"><span class="pdf-attach-name">${escapeHtml(pdf.name.length > 25 ? pdf.name.slice(0, 23) + "…" : pdf.name)}</span><span class="pdf-attach-meta">${pdf.pages} pages · ${sizeStr}${imgStr}${scanBadge}${methodBadge}</span></span></div>`;
    });
    body += "</div>";
  }
  // ── Reply quotes (user messages) — file badge style, supports array ──
  if (isUser) {
    const quotes = msg.replyQuotes || (msg.replyQuote ? [msg.replyQuote] : []);
    for (const rq of quotes) {
      const rqPreview = rq.replace(/\s+/g, " ").slice(0, 80);
      const rqChars = rq.length;
      const rqLines = rq.split("\n").length;
      body += `<div class="reply-quote-badge" title="${escapeHtml(rq.slice(0, 300))}">
        
        <span class="reply-quote-badge-info">
          <span class="reply-quote-badge-name">${escapeHtml(rqPreview)}${rqChars > 80 ? "…" : ""}</span>
          <span class="reply-quote-badge-meta">${rqChars} chars · ${rqLines} line${rqLines > 1 ? "s" : ""}</span>
        </span></div>`;
    }
    // ── Conversation reference badges ──
    if (msg.convRefs && msg.convRefs.length > 0) {
      for (const cr of msg.convRefs) {
        const crTitle = escapeHtml(cr.title || cr.id);
        body += `<div class="reply-quote-badge conv-ref-badge" title="${escapeHtml(t('chat.convRefTitle', { title: crTitle }))}">
          <span class="reply-quote-badge-icon">@</span>
          <span class="reply-quote-badge-info">
            <span class="reply-quote-badge-name">${crTitle}</span>
            <span class="reply-quote-badge-meta">${escapeHtml(t('chat.convRefMeta'))}</span>
          </span></div>`;
      }
    }
    // ── Peer-message provenance banner ──
    // A KIND_PEER_MSG turn injected by a sibling conversation (via
    // project_message / advisory project_intervene) is a real user-role turn,
    // so WITHOUT this it would render byte-identical to something the human
    // typed. Surface it distinctly + attribute the sender so the arrival is
    // observable (backend stamps `_peerMessage` + `_fromConv` in
    // message_queue.dispatch_next_queued).
    if (msg._peerMessage) {
      const _fromShort = escapeHtml((msg._fromConv || "").slice(0, 8) || "peer");
      // A human operator nudge (backend stamps `_peerHuman`) is attributed to
      // the operator, not a peer conversation — distinct label + styling class.
      const _isHuman = !!msg._peerHuman;
      const _labelKey = _isHuman ? "peer.operatorBanner" : "peer.messageBanner";
      const _fallback = _isHuman
        ? "Message from the project operator"
        : "Message from a peer conversation";
      const _pmLabel = (typeof t === "function" && t(_labelKey) !== _labelKey)
        ? t(_labelKey)
        : _fallback;
      body += `<div class="peer-msg-banner${_isHuman ? " peer-msg-banner-operator" : ""}" title="${escapeHtml(msg._fromConv || "")}">`
            + `<span class="peer-msg-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>`
            + `<span class="peer-msg-text">${_pmLabel} <span class="peer-msg-from">conv ${_fromShort}</span></span></div>`;
    }
  }
  // NOTE: the autopilot run summary is NO LONGER a message — it's a human-only
  // sidecar record (conv.autopilotSummaries[runId]) rendered as the run fold's
  // read-only report PANEL by _buildApSummaryPanel. A legacy `_isAutopilotSummary`
  // assistant row from before the sidecar migration still renders as a plain
  // assistant turn (harmless) — we intentionally no longer special-case it.
  // ── Proactive agent banner ──
  if (msg._proactive) {
    const taskName = msg._proactiveTaskId ? `Task ${(msg._proactiveTaskId || "").slice(0, 8)}` : "Proactive Agent";
    body += `<div class="proactive-banner"><span class="pb-text"><span class="pb-name">${escapeHtml(taskName)}</span> — scheduled execution</span></div>`;
  }
  // ── Swarm auto-continue banner ──
  // This assistant turn was started by the backend (not the user) to deliver
  // sub-agent results that finished after the spawning turn ended. See
  // lib/swarm/integration.py::_start_autocontinue_turn.
  if (msg._swarmAutoContinue) {
    const _acLabel = (typeof t === "function" && t("swarm.autoContinue") !== "swarm.autoContinue")
      ? t("swarm.autoContinue")
      : "Continued automatically after sub-agents finished";
    body += `<div class="proactive-banner"><span class="pb-text">↻ <span class="pb-name">${escapeHtml(_acLabel)}</span></span></div>`;
  }
  // ── Turn provenance (finished message) ──
  //   The awaiting-approval login keeps its own prominent callout; memory
  //   prefetch + preferences + any RESOLVED login fold into one quiet,
  //   collapsible strip (renderTurnProvenanceHtml). Learned-preference cards
  //   (with Confirm/Dismiss) stay separate.
  if (!isUser) {
    body += renderMcpLoginHintHtml(msg._mcpLoginHint);
    body += renderTurnProvenanceHtml(msg);
    if (msg._preferencesLearned) {
      body += renderPreferenceLearnedHtml(msg._preferencesLearned);
    }
  }
  /* ★ Autopilot VU bubble, still streaming and not yet showing content —
   * render a live "composing…" pulse so the eagerly-created bubble isn't
   * blank while the simulated user warms up / investigates. */
  if (msg._isVirtualUser && msg._streamingVu && !msg.content
      && !(msg.toolRounds && msg.toolRounds.length) && !msg.thinking) {
    body += `<div class="stream-status vu-composing"><div class="pulse"></div> `
          + `${escapeHtml(typeof t === "function" ? t("autopilot.warming") : "Autopilot…")}</div>`;
  }
  const rounds = getToolRoundsFromMsg(msg);
  /* ★ FLATTENED (owner directive 2026-07-07): autopilot VU turns now render
   * with the IDENTICAL agent bubble layout — the normal grouped tool panel +
   * thinking block go straight into `body`, exactly like an assistant turn.
   * The former "Private · not sent" dashed zone, the "Sent to the agent" green
   * zone, and the "Autopilot investigation" header are gone; only the avatar +
   * "Autopilot" label distinguish who is speaking. The DATA-layer provenance
   * is UNCHANGED (conv_message_builder._build_user_message still reads ONLY
   * `content` for a VU user row — toolRounds/thinking remain display-only and
   * never reach the next agent); this is a render-layer change only. */
  /* ★ Interleaved segment timeline (epic pt_8b406df8fbe24ae5, step 5).
   *   When this finished message carries a `segments` list, render tools with
   *   their PRECEDING thinking+narration inline (per-tool), instead of the
   *   three grouped blocks. This is now the ONLY render path; the grouped
   *   renderer remains only as the automatic fallback for segment-less rows.
   *   ★ Applies to the assistant path (`!isUser`) AND to autopilot VU turns
   *     (`_isVirtualUser`, role=user): the owner directive is that a VU turn
   *     renders with the IDENTICAL agent bubble — so it must take the SAME
   *     inline timeline, not the grouped panel. The VU row now carries its own
   *     `segments` (assembled backend-side in run_virtual_user off the finished
   *     sub-task and persisted on the message; DISPLAY-ONLY — never reaches the
   *     next agent). This mirrors the existing `_isCritic` body carve-out below.
   *   renderSegmentTimelineHTML returns "" if it can't apply (no segments /
   *   unmatchable toolRounds) → we fall through to the legacy render. When it
   *   DOES render, it already includes the per-batch thinking, so the separate
   *   msg.thinking block below is suppressed (_segTimelineRendered). */
  const _segTimelineAllowed = !isUser || msg._isVirtualUser;
  let _segTimelineRendered = false;
  if (_segTimelineAllowed
      && Array.isArray(msg.segments) && msg.segments.length > 0) {
    const _tl = renderSegmentTimelineHTML(msg.segments, msg, idx);
    if (_tl) {
      body += _tl;
      _segTimelineRendered = true;
    }
  }
  if (!_segTimelineRendered && rounds.length > 0) {
    /* Render any persisted async-swarm inbox chips above the tool panel
       so historical messages still tell the user "this turn received
       async sub-agent updates before the model's reply".               */
    if (msg._inboxInjects && msg._inboxInjects.length) {
      body += _buildSwarmInboxChipsHTML(msg._inboxInjects);
    }
    /* Pass segments so the grouped panel renders per-round narration
     * (translated-in-place) adjacent to each round's tools — the toggle-OFF /
     * timeline-fallback analogue of renderSegmentTimelineHTML. When the
     * timeline DID render (_segTimelineRendered) we never reach here. */
    body += renderToolRoundsHTML(rounds, false, msg.segments);
  }
  /* ★ Windowed/trimmed open: the heavy tool timeline was stripped for transport
   *   (msg._trimmed) to keep first-open tiny. Show a lightweight affordance so
   *   the user can pull the full tool activity on demand — a click hydrates the
   *   FULL conversation (heavy fields refilled by _msgId) and repaints. Only
   *   when this turn actually had tool rounds and none are currently present. */
  if (!_segTimelineRendered && rounds.length === 0 && msg && msg._trimmed
      && msg._trimmedToolRoundCount > 0) {
    const _n = msg._trimmedToolRoundCount;
    const _cid = (typeof activeConvId !== 'undefined') ? activeConvId : '';
    const _label = (typeof t === 'function')
      ? t('convWindow.loadToolActivity', { n: _n }) : `Load tool activity (${_n})`;
    const _tlIcon = (typeof Icon === 'function') ? Icon('wrench', 13) : '';
    body += `<div class="trimmed-tool-activity" onclick="hydrateFullConversation('${escapeHtml(_cid)}')" title="${escapeHtml(_label)}">${_tlIcon}<span>${escapeHtml(_label)}</span></div>`;
  }
  /* ── Terminal thinking block ──────────────────────────────────────────
   * `msg.thinking` is the TERMINAL round's reasoning (the prose the model
   * produced right before its final answer — task['thinking'], reset each
   * tool round; the per-round reasoning of earlier batches lives in the
   * segments, NOT here). Render it whenever present.
   *
   * ★ ROOT-FIX (recurring "reasoning_content missing" bug): the block used to
   * be gated on `!_segTimelineRendered` under the assumption that the timeline
   * "already includes the thinking". That is TRUE only for the per-round
   * (non-terminal) thinking segments — the timeline DELIBERATELY skips every
   * `terminal` segment (renderSegmentTimelineHTML: `if (s.terminal) continue`),
   * exactly like the deliverable text is excluded there and rendered separately
   * by the `else if (msg.content)` branch above. So when the timeline rendered,
   * the terminal reasoning had NO render path at all and was silently dropped.
   * The terminal thinking string is distinct from every inline per-round
   * segment (different accumulator), so rendering it here never duplicates the
   * inline thinking. See tests/test_frontend_terminal_thinking_render.py for
   * the load-bearing guard (NEUTER re-adds the gate → terminal thinking drops). */
  if (msg.thinking) {
    const thinkLen = msg.thinking.length;
    const thinkMeta = thinkLen >= 1024 ? ` (${Math.round(thinkLen / 1024)}k chars)` : ` (${thinkLen} chars)`;
    const _thinkLbl = (typeof t === 'function') ? t('stream.thinking.done') : 'Thinking Process';
    body += `<div class="thinking-block" onclick="_toggleThinking(this,_msgElIndex(this))"><div class="thinking-header"><span class="thinking-label">${escapeHtml(_thinkLbl)}${thinkMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // ── Prior thinking (display-only) ──
  // Trailing reasoning that was emitted after the last completed tool batch
  // and discarded on Continue rollback.  Cannot be replayed on the wire
  // (Anthropic rejects orphan thinking blocks; OpenAI-compat strips it),
  // so the field is stripped by lib.llm_sanitize._strip_non_api_fields
  // before any LLM call.  We surface it here so the user can still see what
  // the model was reasoning about right before the resume.
  if (!isUser && msg.priorThinking) {
    const priorLen = msg.priorThinking.length;
    const priorMeta = priorLen >= 1024 ? ` (${Math.round(priorLen / 1024)}k chars)` : ` (${priorLen} chars)`;
    body += `<div class="thinking-block thinking-prior" onclick="_togglePriorThinking(this,_msgElIndex(this))"><div class="thinking-header"><span class="thinking-label">Earlier Thinking${priorMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // ── Prior response (display-only) ──
  // The free-form prose the model wrote after the last completed tool batch,
  // discarded on Continue rollback (the LLM regenerates from the tool-result
  // checkpoint, so this tail can't be replayed on the wire — see priorThinking
  // above for the identical wire-safety contract).  Surfacing it keeps the
  // rollback visible instead of silently dropping the content area while the
  // tool panel stays put.
  if (!isUser && msg.priorContent) {
    const priorCLen = msg.priorContent.length;
    const priorCMeta = priorCLen >= 1024 ? ` (${Math.round(priorCLen / 1024)}k chars)` : ` (${priorCLen} chars)`;
    body += `<div class="thinking-block thinking-prior content-prior" onclick="_togglePriorContent(this,_msgElIndex(this))"><div class="thinking-header"><span class="thinking-label">Earlier Response${priorCMeta} · rolled back</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // Track which branches have been inlined (rendered right after their anchor text)
  let _inlinedBranches = new Set();
  // ── Image Generation error card (from _igError metadata) ──
  // Renders a styled, color-coded error card based on error type.
  if (!isUser && msg._igError) {
    const ige = msg._igError;
    // Determine error-type CSS class and icon
    let errTypeClass = 'ig-error-generic';
    let errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
    if (ige.isRateLimit || ige.errorType === 'rate_limited') {
      errTypeClass = 'ig-error-ratelimit';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>';
    } else if (ige.isContentBlocked || ige.errorType === 'content_blocked') {
      errTypeClass = 'ig-error-blocked';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>';
    } else if (ige.isTimeout || ige.errorType === 'timeout') {
      errTypeClass = 'ig-error-timeout';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/></svg>';
    } else if (ige.errorType === 'no_slot') {
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z"/><path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/><path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0Z"/></svg>';
    }
    body += `<div class="ig-result-wrapper">
      <div class="ig-error-card ${errTypeClass}">
        <div class="ig-error-icon">${errIcon}</div>
        <div class="ig-error-title">${escapeHtml(ige.title || 'Image generation failed')}</div>
        <div class="ig-error-text">${escapeHtml(ige.text || '')}</div>
        ${ige.detail ? `<div class="ig-error-detail">${escapeHtml(ige.detail)}</div>` : ''}
        ${ige.blockReason ? `<div class="ig-error-detail">Block reason: ${escapeHtml(ige.blockReason)}</div>` : ''}
        <button class="ig-retry-btn" onclick="_igRetryLastPrompt()">Retry</button>
      </div>
    </div>`;
  // ── Image Generation result card (from _igResult metadata) ──
  // When an assistant message has _igResult, render a styled card instead of raw markdown
  // so the card survives renderChat re-renders (e.g. when user sends a second image prompt).
  } else if (!isUser && msg._igResult && msg._igResult.image_url) {
    const ig = msg._igResult;
    const imgSrc = ig.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(ig.image_url) : ig.image_url) : ig.image_url;
    const promptText = ig.prompt || '';
    const promptShort = promptText.length > 80 ? promptText.slice(0, 80) + '…' : promptText;
    const sizeStr = ig.file_size ? (typeof _formatFileSize === 'function' ? _formatFileSize(ig.file_size) : Math.round(ig.file_size / 1024) + ' KB') : '';
    const elapsedStr = ig.elapsed ? ig.elapsed + 's' : '';
    const arStr = ig.aspect_ratio || '';
    body += `<div class="ig-result-wrapper">
      <div class="ig-result-card">
        <img src="${imgSrc}" alt="${escapeHtml(promptText.slice(0,100))}"
             onclick="_openImageFullscreen(this.src)" />
        <div class="ig-result-footer">
          <span class="ig-result-prompt" title="${escapeHtml(promptText)}">${escapeHtml(promptShort)}</span>
          <div class="ig-result-meta">
            ${ig.model ? `<span class="ig-meta-pill">${escapeHtml(ig.model)}</span>` : ''}
            ${ig.provider_id ? `<span class="ig-meta-pill">@${escapeHtml(ig.provider_id)}</span>` : ''}
            ${arStr ? `<span class="ig-meta-pill">${escapeHtml(arStr)}</span>` : ''}
            ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
            ${elapsedStr ? `<span class="ig-meta-pill">${elapsedStr}</span>` : ''}
            ${ig.history_turns ? `<span class="ig-meta-pill ig-history-pill" title="${ig.history_turns} prior editing turn${ig.history_turns > 1 ? 's' : ''}">${Icon('refresh', 11)} ${ig.history_turns}</span>` : ''}
          </div>
          <div class="ig-result-actions">
            <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
            <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
          </div>
        </div>
      </div>
    </div>`;
    // Also render any text content from the model (e.g. revised prompt)
    const textContent = (msg.content || '').replace(/!\[Generated Image\]\([^)]*\)\s*/g, '').trim();
    if (textContent) {
      body += `<div class="md-content">${renderMarkdown(textContent)}</div>`;
    }
  // ── Batch Image Generation results grid (from _igResults array) ──
  } else if (!isUser && msg._igResults && msg._igResults.length > 0) {
    const results = msg._igResults;
    // Skip rendering "pending" placeholders during active batch (DOM has live slots)
    const isPending = msg._igBatchPending && results.every(r => r.error === 'pending');
    if (isPending) {
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">Generating…</div></div>`;
    } else {
      const okResults = results.filter(r => r.ok && r.image_url);
      const cols = Math.min(results.length, 2);
      const _fmtSize = typeof _formatFileSize === 'function' ? _formatFileSize : (b => b > 0 ? Math.round(b / 1024) + ' KB' : '');
      const _shortModel = typeof _IG_MODEL_SHORT !== 'undefined' ? _IG_MODEL_SHORT : {};
      const distinctModels = new Set(results.map(r => r.model)).size;
      const bannerLabel = distinctModels > 1 ? t('chat.igBatchAll', { n: results.length }) : t('chat.igBatchN', { n: results.length });
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">${escapeHtml(bannerLabel)} · ${escapeHtml(t('chat.igBatchSuccess', { ok: okResults.length, total: results.length }))}</div><div class="ig-batch-grid ig-cols-${cols}">`;
      for (let ri = 0; ri < results.length; ri++) {
        const r = results[ri];
        if (r.ok && r.image_url) {
          const imgSrc = r.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(r.image_url) : r.image_url) : r.image_url;
          const sizeStr = r.file_size ? _fmtSize(r.file_size) : '';
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-result-card">
              <img src="${imgSrc}" alt="${escapeHtml((r.prompt || '').slice(0,60))}"
                   onclick="_openImageFullscreen(this.src)" />
              <div class="ig-result-footer">
                <span class="ig-result-prompt">${escapeHtml(modelLabel)}</span>
                <div class="ig-result-meta">
                  ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
                  ${r.elapsed ? `<span class="ig-meta-pill">${r.elapsed}s</span>` : ''}
                </div>
                <div class="ig-result-actions">
                  <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
                  <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
                </div>
              </div>
            </div>
          </div>`;
        } else if (r.error === 'pending') {
          // Still pending — show mini spinner
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-generating ig-batch-loading">
              <div class="ig-gen-spinner"></div>
              <div class="ig-gen-title">${escapeHtml(modelLabel)}</div>
              <div class="ig-gen-subtitle">Pending…</div>
            </div>
          </div>`;
        } else {
          // Error slot — with error-type differentiation and retry button
          const errModel = _shortModel[r.model] || r.model || '?';
          let errTypeClass = 'ig-error-generic';
          let errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
          const et = r.errorType || '';
          if (et === 'rate_limited') { errTypeClass = 'ig-error-ratelimit'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>'; }
          else if (et === 'content_blocked') { errTypeClass = 'ig-error-blocked'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>'; }
          else if (et === 'timeout') { errTypeClass = 'ig-error-timeout'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/></svg>'; }
          const promptEsc = JSON.stringify(r.prompt || '').replace(/"/g, '&quot;');
          const modelEsc = JSON.stringify(r.model || '').replace(/"/g, '&quot;');
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}"><div class="ig-batch-error ${errTypeClass}">
            <div class="ig-error-icon">${errIcon}</div>
            <div class="ig-error-title">${escapeHtml(errModel)}</div>
            <div class="ig-error-text">${escapeHtml((r.error || 'Failed').slice(0,200))}</div>
            <button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(_msgElIndex(this),${ri},${promptEsc},${modelEsc})" title="Retry this slot">↻ Retry</button>
          </div></div>`;
        }
      }
      body += `</div></div>`;
    }
  } else if (!isUser && msg._isCompactionSummary) {
    // Manual /compact boundary card — built by a standalone, testable fn.
    body += buildCompactionCardHtml(msg);
  } else if (msg.content) {
    try {
      let mdHtml;
      // Content vs translation are now two independent decisions:
      //   _showTrans → render the 译文; else render the resolved content origin
      //   (_disp: markdown for assistant/critic/VU, escaped 源文 for a normal
      //   user). The old _isCritic special-case is gone — displayContent owns
      //   the content-origin inversion.
      if (_showTrans) {
        mdHtml = renderMarkdown(stripNoTranslateTags(_tr.text));
      } else if (_disp.isMarkdown) {
        mdHtml = renderMarkdown(_disp.text);
      } else {
        mdHtml = escapeHtml(stripNoTranslateTags(_disp.text));
      }
      // ── Inject anchored branch pills inline (assistant only) ──
      if (!isUser && msg.branches?.length) {
        const r = _injectAnchoredBranches(mdHtml, msg, idx);
        mdHtml = r.html;
        _inlinedBranches = r.inlinedSet;
      }
      /* ★ FLATTENED (owner directive 2026-07-07): the VU reply renders as a
       * plain md-content body, identical to an agent turn — no "Sent to the
       * agent" green handoff zone. DATA-layer provenance is unchanged (only
       * `content` reaches the next agent; see conv_message_builder). */
      body += `<div class="md-content${isUser ? " user-content" : ""}">${mdHtml}</div>`;
    } catch (e) {
  // ── Compaction markers — render inline chips for each archived snapshot ──
  // Each marker becomes a clickable chip that opens the Compaction Viewer
  // (window.openCompactionViewer in static/js/compaction-viewer.js). We
  // intentionally render in render-time order; clicking is delegated through
  // a global handler so the chips survive innerHTML replacement.
  if (!isUser && Array.isArray(msg._compactions) && msg._compactions.length) {
    const _ccDom = msg._compactions.map((c) => {
      const trig = (c.trigger || 'force');
      const _zapSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';
      const _wrenchSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></svg>';
      const _archiveSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>';
      const trigLabel = trig === 'reactive'
        ? _zapSvg + ' ' + escapeHtml(t('chat.compactReactive'))
        : (trig === 'manual' ? _wrenchSvg + ' ' + escapeHtml(t('chat.compactManual')) : _archiveSvg + ' ' + escapeHtml(t('chat.compactAuto')));
      const before = c.tokensBefore || 0;
      const after  = c.tokensAfter  || 0;
      const reductionPct = (c.reductionPct != null) ? c.reductionPct
                          : (before > 0 ? Math.round((1 - after / before) * 100) : 0);
      const sizeFrag = before > 0 && after > 0
        ? `${(before/1000).toFixed(0)}k → ${(after/1000).toFixed(0)}k tokens · -${reductionPct}%`
        : (before > 0 ? t('chat.compactArchivedTokens', { k: (before/1000).toFixed(0) }) : t('chat.compactArchived'));
      const reasonFrag = c.reason ? `<span class="compaction-marker-reason">${escapeHtml(c.reason)}</span>` : '';
      const statusCls = (c.status === 'done') ? 'is-done' : 'is-progress';
      const archiveAttr = (c.archiveId == null) ? '' : `data-archive-id="${c.archiveId}"`;
      const convAttr = `data-conv-id="${escapeHtml(c.convId || '')}"`;
      return `<button type="button" class="compaction-marker ${statusCls}"
        ${archiveAttr} ${convAttr}
        onclick="if(window.openCompactionViewer){window.openCompactionViewer(this.dataset.convId, parseInt(this.dataset.archiveId,10))}else{console.warn('compaction-viewer not loaded')}"
        title="${escapeHtml(t('chat.compactViewSnapshot'))}">
        <span class="compaction-marker-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg></span>
        <span class="compaction-marker-trigger">${trigLabel}</span>
        <span class="compaction-marker-stat">${escapeHtml(sizeFrag)}</span>
        ${reasonFrag}
        <span class="compaction-marker-cta">${escapeHtml(t('chat.compactViewHistory'))}</span>
      </button>`;
    }).join('');
    body += `<div class="compaction-marker-row">${_ccDom}</div>`;
  }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${escapeHtml(msg.content)}</div>`;
    }
  }
  // ── Bilingual display ──
  if (isUser && msg.originalContent && msg.originalContent !== msg.content) {
    const _tmUser = _tr.model ? `<span class="bilingual-model" title="${escapeHtml(_tr.model)}">${escapeHtml(_tr.model)}</span>` : '';
    // Strip any leaked <notranslate>/<nt> tags from the translation display.
    const _userTrans = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-translated"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type">${escapeHtml(t('chat.bilingualOriginal'))}</span><span class="bilingual-sep">/</span><span class="bilingual-type active">${escapeHtml(t('chat.bilingualTranslated'))}</span>${_tmUser}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'user',_msgElIndex(this))" title="Copy translation"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content user-content">${escapeHtml(_userTrans)}</div></div></div>`;
  }
  // ── Auto-translate failed notice (user messages) ──
  // The send-path auto-translate was attempted but failed / timed out, so the
  // ORIGINAL (untranslated) text was sent to the model. We surface a quiet,
  // non-blocking notice instead of silently dropping the translation — click
  // to retranslate just this message.
  if (isUser && !msg._isEndpointReview && !msg.originalContent && _tr.sendFailed) {
    const _failKind = _tr.sendFailed === 'timed_out' ? 'timed_out' : 'failed';
    const _failKey = `translate.sendFailed.${_failKind}`;
    const _failMsg = (typeof t === 'function' && t(_failKey) !== _failKey)
      ? t(_failKey)
      : (_failKind === 'timed_out'
          ? 'Auto-translate timed out — original text was sent'
          : 'Auto-translate failed — original text was sent');
    const _retryTip = (typeof t === 'function' && t('translate.sendFailed.retry') !== 'translate.sendFailed.retry')
      ? t('translate.sendFailed.retry') : 'Retry';
    // Retry = re-run the turn: regenerateFromUser re-translates the original
    // text AND regenerates the response (translateMessage rejects plain user
    // messages and wouldn't change what the model already received).
    body += `<div class="translate-failed-notice" onclick="event.stopPropagation();regenerateFromUser(_msgElIndex(this))" title="${escapeHtml(_failMsg)}">`
      + `<svg class="tfn-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`
      + `<span class="tfn-text">${escapeHtml(_failMsg)}</span>`
      + `<span class="tfn-retry">${escapeHtml(_retryTip)}</span>`
      + `</div>`;
  }
  if (!isUser && _showTrans) {
    const _tmAsst = _tr.model ? `<span class="bilingual-model" title="${escapeHtml(_tr.model)}">${escapeHtml(_tr.model)}</span>` : '';
    // Defense in depth — strip any leaked <notranslate>/<nt> tags.
    const _asstOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">${escapeHtml(t('chat.bilingualOriginal'))}</span><span class="bilingual-sep">/</span><span class="bilingual-type">${escapeHtml(t('chat.bilingualTranslated'))}</span>${_tmAsst}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'assistant',_msgElIndex(this))" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_asstOrig)}</div></div></div>`;
  }
  // ── Critic (endpoint review) / Autopilot VU bilingual block — symmetric with assistant ──
  // (isUser && _showTrans) is true ONLY for critic/VU users — a normal user has
  // _disp.isMarkdown=false → _showTrans=false — so this stays exclusive with the
  // assistant block above while keeping its distinct 'critic' copy lane.
  if (isUser && _showTrans) {
    const _tmCritic = _tr.model ? `<span class="bilingual-model" title="${escapeHtml(_tr.model)}">${escapeHtml(_tr.model)}</span>` : '';
    const _critOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">${escapeHtml(t('chat.bilingualOriginal'))}</span><span class="bilingual-sep">/</span><span class="bilingual-type">${escapeHtml(t('chat.bilingualTranslated'))}</span>${_tmCritic}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'critic',_msgElIndex(this))" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_critOrig)}</div></div></div>`;
  }
  // ── Persistent "translating..." indicator (survives re-render / tab switch) ──
  // Owned by ui/translation_indicator.js, which reads translation state via the
  // canonical model. chat_render no longer touches _translate* fields here.
  if (typeof renderTranslateIndicator === 'function') {
    body += renderTranslateIndicator(msg, idx, { segTimelineRendered: _segTimelineRendered });
  }
  if (msg.error)
    body += renderErrorEnvelope(msg.error);
  if (!isUser) body += renderFileChangesBar(msg, idx);
  /* ── Renderable artifact chips (md/html/svg) ─────────────────────────
   * Hooked from artifacts.js.  Persisted as a first-class row in
   * chat_artifacts so the chip survives compaction.  See lib/artifacts/. */
  if (!isUser && typeof window.Artifacts !== "undefined"
      && Array.isArray(msg._artifacts) && msg._artifacts.length > 0) {
    try { body += window.Artifacts.renderChips(msg._artifacts); }
    catch (e) { console.debug("[Artifacts] renderChips failed:", e); }
  }
  if (!isUser) {
    /* Is this the still-running tail? The backend withholds finishReason/
     * usage mid-stream, so a model-only assistant message that is the last
     * message of a conv with a live stream / active task is in progress —
     * suppress its premature finish bar (see renderFinishInfo). */
    const _lvConv = getActiveConv();
    const _isLiveTail = !!(_lvConv
      && idx === _lvConv.messages.length - 1
      && (activeStreams.has(_lvConv.id) || _lvConv.activeTaskId));
    body += renderFinishInfo(msg, _isLiveTail);
  }
  const idAttr = typeof idx === "number" ? ` id="msg-${idx}"` : "";
  /* Stable per-message handle for the unified chatInner controller.
   * Server backfills `_msgId` (UUID) on persist; client-only messages get
   * a `tmp_<...>` id from _newClientMsgId().  Either way, when present we
   * mirror it onto the DOM so future surgical updates can address by id
   * instead of mutable index. */
  const msgIdAttr = (msg && msg._msgId) ? ` data-msg-id="${escapeHtml(msg._msgId)}"` : "";
  /* ★ Autopilot run fold: stamp the run id on every turn of an autopilot
   * run (VU turns + the close-out summary) so renderChat's post-render
   * grouping pass can collapse a CONCLUDED run's VU<->agent transcript into
   * one <details>, leaving [original human msg → summary] as the visible
   * spine. Grouping keys on this EXPLICIT id — never role-scanning. */
  const apRunAttr = (msg && msg._autopilotRunId)
    ? ` data-ap-run="${escapeHtml(msg._autopilotRunId)}"` : "";
  const apSummaryAttr = (msg && msg._isAutopilotSummary) ? ` data-ap-summary="1"` : "";
  let actionBtns = "";
  if (typeof idx === "number") {
    /* i18n: label/tooltip resolver — defensive against isolated harnesses
     * where `t` may be undefined (falls back to the English string). */
    const _mt = (k, fallback) => (typeof t === 'function' && t(k) !== k) ? t(k) : fallback;
    const copyH = `<button class="msg-action-btn copy-msg-btn" onclick="event.stopPropagation();copyMessage(_msgElIndex(this))" title="${escapeHtml(_mt('msgAction.copyTitle', 'Copy'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> ${escapeHtml(_mt('msgAction.copy', 'Copy'))}</button>`;
    /* ★ Regen is a USER-LANE action: it truncates after this turn and re-runs
     *   the agent's response from here. That is meaningful on EVERY user-lane
     *   bubble — a real human turn, a role=user peer-message turn, AND an
     *   autopilot VU driver turn (owner directive 2026-07-17: re-running the
     *   agent's reply to an autopilot step is exactly what you want when that
     *   reply was bad; on an in-flight run regenerateFromUser hard-cancels the
     *   racing stream first). So Regen shows on any user-lane turn. */
    const _showRegen = isUser;
    /* Edit is available on EVERY bubble now (user, agent, autopilot VU,
     * critic, planner). For a real human user turn it opens the full editor
     * (Save / Save & Resend); for every other lane it is EDIT-IN-PLACE only
     * (Save) — startEditMessage / saveEditOnly branch on the role. Regen
     * shows on any user-lane turn (see _showRegen below). */
    const editH = `<button class="msg-action-btn" onclick="event.stopPropagation();startEditMessage(_msgElIndex(this))" title="${escapeHtml(_mt('msgAction.editTitle', 'Edit'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> ${escapeHtml(_mt('msgAction.edit', 'Edit'))}</button>`;
    const regenH = _showRegen
      ? `<button class="msg-action-btn msg-regen-btn" onclick="event.stopPropagation();regenerateFromUser(_msgElIndex(this))" title="${escapeHtml(_mt('msgAction.regenTitle', 'Regenerate response from this message'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> ${escapeHtml(_mt('msgAction.regen', 'Regen'))}</button>`
      : "";
    const conv_ = getActiveConv();
    /* ★ Continue is a RESUME affordance for an INTERRUPTED/TRUNCATED turn —
     *   it POSTs to /api/chat/continue to pick up from the last tool-call
     *   checkpoint. On a CLEAN finish (end_turn/stop/stop_sequence — the same
     *   reasons that earn the green ✓ in finish_info.js) there is nothing to
     *   resume, so showing it is meaningless and misleading. Gate it as the
     *   exact complement of that ✓: only offer Continue when the last
     *   assistant turn did NOT finish normally (length / max_tokens /
     *   tool_rounds_exhausted / premature_close / aborted / interrupted / …).
     *   A missing finishReason (legacy / unknown) still shows it, so we never
     *   silently drop the recovery path for a genuinely-truncated old turn. */
    const _FINISH_CLEAN = ["stop", "end_turn", "stop_sequence"];
    const isLastAssistant =
      !isUser &&
      conv_ &&
      idx === conv_.messages.length - 1 &&
      !activeStreams.has(conv_.id);
    const _turnFinishedClean = _FINISH_CLEAN.includes(msg.finishReason);
    const continueH = (isLastAssistant && !_turnFinishedClean)
      ? `<button class="msg-action-btn msg-continue-btn" onclick="event.stopPropagation();continueAssistant()" title="${escapeHtml(_mt('msgAction.continueTitle', 'Continue generating from where it left off'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> ${escapeHtml(_mt('msgAction.continue', 'Continue'))}</button>`
      : "";
    const isShowingTrans = _tr.showing;
    // Show the Translate button on: (a) assistant messages, (b) endpoint
    // critic review messages (role=user + _isEndpointReview) — they
    // receive auto-translate too.
    const _translateBtnAllowed = !isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser));
    const translateH = _translateBtnAllowed
      ? `<button class="msg-action-btn msg-translate-btn${isShowingTrans ? " translated" : ""}" onclick="event.stopPropagation();translateMessage(_msgElIndex(this))" title="${escapeHtml(isShowingTrans ? _mt('msgAction.showOriginalTitle', 'Show Original') : _mt('msgAction.translateTitle', 'Translate'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0014.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg> ${escapeHtml(isShowingTrans ? _mt('msgAction.original', 'Original') : _mt('msgAction.translate', 'Translate'))}</button>`
      : "";
    const exportImgH = !isUser
      ? `<button class="msg-action-btn msg-export-img-btn" onclick="event.stopPropagation();ExportImages.exportMessageWithPreview(_msgElIndex(this))" title="${escapeHtml(_mt('msgAction.exportTitle', 'Export as phone-screen images'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg> ${escapeHtml(_mt('msgAction.export', 'Export'))}</button>`
      : "";
    const canDelete = conv_ && !activeStreams.has(conv_.id) && !conv_.activeTaskId;
    const deleteH = canDelete
      ? `<button class="msg-action-btn msg-delete-btn" onclick="event.stopPropagation();deleteTurn(_msgElIndex(this))" title="${escapeHtml(isUser ? _mt('msgAction.deleteTurnTitle', 'Delete this turn') : _mt('msgAction.deleteMsgTitle', 'Delete this message'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>`
      : "";
    /* ★ Branch ("分支") is an assistant-lane action — moved out of the separate
     *   dashed `.branch-zone` pill into the unified bottom action bar so it
     *   shares the `.msg-action-btn` styling/hover-reveal with Copy/Edit/…. */
    const _branchLabel = _mt('branch.add', 'Branch');
    const branchBtnH = !isUser
      ? `<button class="msg-action-btn msg-branch-btn" onclick="event.stopPropagation();promptNewBranch(_msgElIndex(this))" title="${escapeHtml(_branchLabel)}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg> ${escapeHtml(_branchLabel)}</button>`
      : "";
    actionBtns = `<div class="message-actions">${copyH}${editH}${regenH}${continueH}${translateH}${exportImgH}${branchBtnH}${deleteH}</div>`;
  }
  // ★ Tofu mascot avatars: Worker gets worker tofu, Planner gets planner tofu
  let avatarContent = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : "✦",
    roleName = "Agent";
  if (msg._isEndpointPlanner) {
    avatarContent = (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
    roleName = "Planner";
  }
  // ── Assistant-lane auto-initiation (swarm auto-continue): a turn the backend
  //    started to drain sub-agent updates is NOT a human-initiated agent turn,
  //    so attribute it via the shared registry instead of the plain "Agent". ──
  let _initBadge = "";
  if (!isUser && !msg._isEndpointPlanner && typeof _initiatorPresentation === 'function') {
    const _ip = _initiatorPresentation(msg);
    if (_ip && _ip.lane === 'assistant') {
      avatarContent = _ip.avatar;
      roleName = _ip.label;
      _initBadge = `<span class="ep-verdict-badge init-badge ${_ip.cls}">${escapeHtml(_ip.label)}</span>`;
    }
  }

  // ── Branch zone for assistant messages (only un-inlined branches + add button) ──
  let branchHtml = "";
  if (!isUser && typeof renderBranchZone === "function") {
    branchHtml = renderBranchZone(msg, idx, _inlinedBranches);
  }

  // ── Planner badge for planner messages ──
  let plannerBadge = "";
  if (msg._isEndpointPlanner) {
    plannerBadge = `<span class="ep-verdict-badge ep-verdict-planner">Plan</span>`;
  }

  // ── Critic verdict badge for endpoint review messages ──
  let criticBadge = "";
  if (msg._isEndpointReview) {
    if (msg._epApproved) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stop">Approved</span>`;
    } else if (msg._isStuck) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stuck">Stuck</span>`;
    } else if (msg._epNextPhase === 'planner') {
      // Critic requested a full re-plan — distinct amber badge
      criticBadge = `<span class="ep-verdict-badge ep-verdict-replan">Replan</span>`;
    } else {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-continue">Iteration ${msg._epIteration || ""}</span>`;
    }
  }

  // ── Avatar: tofu critic for reviews & autopilot, onigiri mascot for user ──
  const _criticAvatar = (typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  // A peer/operator KIND_PEER_MSG turn is role=user but NOT human input — give
  // it the people-glyph (mirrors the project_peer_status tool icon) + a role
  // label instead of the onigiri + "You", so the header identity stops lying.
  // The specific sender (conv id / operator) stays in the in-bubble banner.
  const _peerAvatar = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
  const _tOr = (k, fb) => (typeof t === "function" && t(k) !== k) ? t(k) : fb;
  // A user-lane turn auto-initiated by proactive / timer / brain must NOT show
  // the onigiri "You". Resolve those through the shared registry; the existing
  // critic/VU/peer arms keep their dedicated (test-pinned) avatars + labels.
  const _userInit = (isUser && !msg._isEndpointReview && !msg._isVirtualUser
                     && !msg._peerMessage && typeof _initiatorPresentation === 'function')
    ? _initiatorPresentation(msg) : null;
  const userAvatar = (msg._isEndpointReview || msg._isVirtualUser)
    ? _criticAvatar
    : msg._peerMessage
    ? _peerAvatar
    : (_userInit && _userInit.lane === 'user')
    ? _userInit.avatar
    : (typeof _USER_AVATAR_SVG !== 'undefined') ? _USER_AVATAR_SVG
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
  const userLabel = msg._isEndpointReview ? "Critic"
    : msg._isVirtualUser ? "Autopilot"
    : msg._peerMessage
    ? (msg._peerHuman ? _tOr("peer.operatorLabel", "Operator") : _tOr("peer.senderLabel", "Peer"))
    : (_userInit && _userInit.lane === 'user')
    ? _userInit.label
    : "You";

  /* messageTime is a formatter output (digits + localized separators) —
   * escape it by default via safeHtml (it carries no markup). */
  const messageTimeHtml = messageTime ? safeHtml`<span class="message-time">${messageTime}</span>` : '';
  /* Escape the fingerprint for attribute storage: it now folds in fields that
   * can contain HTML-special chars (e.g. a run_command round's `title` = the
   * raw shell command, with literal `"`). A plain-string `raw()` splice let a
   * quote close the attribute early and spill the rest of the fingerprint into
   * the DOM as visible text. `safeHtml` &quot;-escapes it; the surgical-diff
   * read at line ~636 uses getAttribute(), which returns the browser-DECODED
   * value, so the escaped storage stays byte-equal to _msgFingerprint(msg). */
  const mfpAttr = typeof idx === "number" ? safeHtml` data-mfp="${_msgFingerprint(msg)}"` : "";
  const epWorkerCls = (!isUser && !msg._isEndpointPlanner && !msg._isEndpointReview) ? ' ep-worker-msg' : '';
  const epPlannerCls = msg._isEndpointPlanner ? ' ep-planner-msg' : '';
  const vuCls = msg._isVirtualUser ? ' vu-user-msg' : '';
  // ── Per-turn context capsule (floats in the RIGHT GUTTER beside the turn) ──
  // Frozen snapshot of the workspace/tools/model active when this turn was
  // sent (msg._ctx, captured in the send pipeline). Rendered as a direct
  // child of `.message` — NOT inside `.message-content` (which clips via
  // overflow:hidden) — so CSS can place it at left:100% out in the gutter,
  // clear of both the bubble and the hover action-bar. See info-rail.js.
  let turnCtxHtml = "";
  if (msg._ctx && typeof renderTurnCtxNote === "function") {
    try { turnCtxHtml = renderTurnCtxNote(msg._ctx); }
    catch (e) { console.debug("[turnCtx] renderTurnCtxNote failed:", e); }
  }
  const badgeHtml = plannerBadge || criticBadge || _initBadge;
  /* ★ Failed / interrupted turn → REVEAL the bottom action bar without hover.
   *   The base `.message-actions` is an opacity:0 hover-reveal, so on a
   *   conversation that errored or was interrupted the user has to scroll to
   *   the bottom AND hover (impossible on touch) to discover Continue / Retry.
   *   That's the exact reported pain point. Stamping `turn-failed` here lets
   *   CSS pin the bar visible (opacity:1) for a non-user turn carrying an
   *   error envelope or a finishReason=interrupted, so the bottom-anchored
   *   Continue is always discoverable on precisely the turn that needs it.
   *   Scoped to assistant-lane turns; a settled/successful turn keeps the
   *   quiet hover-reveal. */
  const _turnFailed = !isUser && (!!msg.error || msg.finishReason === 'interrupted');
  const _failedCls = _turnFailed ? ' turn-failed' : '';
  /* Queued-pending: a cross-device queued user message that has landed in the
   *   body but is NOT yet dispatched (its turn runs after the current one
   *   finishes). Give it a distinct, muted "queued" affordance so the other
   *   device sees the message AND its real state — not an indistinguishable
   *   sent bubble. The flag is cleared server-side by dispatch_next_queued's
   *   idempotent reconcile, and the _msgFingerprint fold above re-renders this
   *   row into a normal bubble at that point. Scoped to user-lane rows. */
  const _pendingQueued = isUser && !!msg._pendingQueued;
  const _pendingQueuedCls = _pendingQueued ? ' pending-queued' : '';
  /* Small inline "queued" indicator (clock SVG, never emoji — CLAUDE.md §3.4)
   *   shown in the header beside the label. */
  const _queuedIndicator = _pendingQueued
    ? raw(`<span class="queued-indicator icon-box" title="${_tOr('sidebar.queued', 'Queued — sends after the current reply')}" aria-label="${_tOr('sidebar.queued', 'Queued — sends after the current reply')}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span class="queued-indicator-text">${_tOr('sidebar.queued', 'Queued')}</span></span>`)
    : raw('');
  /* Final assembly via safeHtml. roleName / userLabel / time are
   * escaped by default. Everything pre-built above (avatars = trusted
   * SVG, badgeHtml, body, branchHtml, actionBtns, the class/attr
   * fragments) is already-trusted HTML, marked raw(). */
  const _classAttr = `${isUser ? ' user-msg' : ''}${_pendingQueuedCls}${msg._isEndpointReview ? ' ep-critic-msg' : ''}${epPlannerCls}${epWorkerCls}${vuCls}${_failedCls}`;
  return String(safeHtml`<div class="message${raw(_classAttr)}"${raw(idAttr)}${raw(msgIdAttr)}${raw(apRunAttr)}${raw(apSummaryAttr)}${raw(mfpAttr)}><div class="message-avatar">${raw(isUser ? userAvatar : avatarContent)}</div><div class="message-content"><div class="message-header"><span class="message-role">${isUser ? userLabel : roleName}</span>${raw(badgeHtml)}${_queuedIndicator}${raw(messageTimeHtml)}</div><div class="message-body">${raw(body)}</div>${raw(branchHtml)}${raw(actionBtns)}</div>${raw(turnCtxHtml)}</div>`);
}
