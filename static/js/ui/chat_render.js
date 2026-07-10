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

/* The run's close-out REPORT — only when the record carries report content
 * (a clean [VU: TASK_DONE]). A manual-stop concluded record has a status but
 * NO content, so this returns null for it (the run still folds, but shows no
 * report panel — the "Summarize this run" affordance appears instead). */
function _apRunSummary(conv, runId) {
  const rec = _apRunRecord(conv, runId);
  return (rec && rec.content) ? rec : null;
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

/* ★ Post-render DOM pass that docks each concluded run's REPORT as a
 * STANDALONE boundary node — the clean successor to the two-anchor graft.
 * Mirrors `_applyAutopilotRunFolds`: it runs at every render exit (surgical /
 * full / bg-refresh), operates purely on the finished DOM, and is fully
 * idempotent (removes all existing summary nodes, then re-inserts from the
 * current sidecar). The panel is NEVER a `msg-N` node, so renderChat's
 * index-based surgical diff + stale-removal loop ignore it entirely.
 * Placement (see `_apSummaryPlacements`):
 *   • boundary — inserted immediately AFTER the run's last stamped turn's
 *     `#msg-<afterMsgIdx>` element (the run boundary), so the agent's own
 *     work-summary sits at the end of that run, not under a synthetic user
 *     bubble and not detached at the transcript tail.
 *   • tail (compacted/orphaned run) — appended at the end of the message list.
 * A boundary whose `#msg-N` is outside the loaded lazy window is treated as a
 * tail placement so the report is still reachable. */
function _applyAutopilotSummaryPanels(inner, conv) {
  if (!inner) return;
  try {
    // Idempotent: clear any panels a previous pass inserted.
    inner.querySelectorAll(':scope > .ap-summary-panel[data-ap-report-run]').forEach(n => n.remove());
    const placements = _apSummaryPlacements(conv);
    if (!placements.length) return;
    for (const p of placements) {
      const html = _apReportPanelHTML(conv, p.runId);
      if (!html) continue;
      const wrapper = document.createElement('div');
      wrapper.innerHTML = html;
      const node = wrapper.firstElementChild;
      if (!node) continue;
      let anchorEl = null;
      if (!p.tail && typeof p.afterMsgIdx === 'number') {
        anchorEl = document.getElementById('msg-' + p.afterMsgIdx);
      }
      if (anchorEl && anchorEl.parentNode === inner) {
        // Dock right after the run's boundary turn (before the next sibling).
        inner.insertBefore(node, anchorEl.nextSibling);
      } else {
        // Tail placement, or the boundary turn is outside the lazy window.
        inner.appendChild(node);
      }
    }
  } catch (e) {
    console.warn('[Autopilot summary] panel placement failed (non-fatal):', e && e.message);
  }
}

/* Open the run's close-out REPORT in a read-only sub-window (modal overlay).
 * The report is a human-only debrief (mirrors the vu-private-zone framing) —
 * never sent to the agent, never part of the transcript. Reached from the
 * "View report" button in the autopilot run fold's summary card. Idempotent:
 * a second call replaces any open report modal. Closes on overlay click, the
 * × button, or Escape. */
function _openApSummaryModal(runId) {
  try {
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    const rec = _apRunRecord(conv, runId);
    if (!rec || !rec.content) {
      console.warn('[Autopilot report] no report content for run', runId);
      return;
    }
    /* Drop any previously-open report modal. */
    const prev = document.getElementById('apReportModal');
    if (prev) prev.remove();

    const _title = (typeof t === 'function' && t('autopilot.summaryLabel') !== 'autopilot.summaryLabel')
      ? t('autopilot.summaryLabel') : 'Run summary report';
    const _privNote = (typeof t === 'function' && t('autopilot.summaryHumanOnly') !== 'autopilot.summaryHumanOnly')
      ? t('autopilot.summaryHumanOnly') : 'For you only · not sent to the agent';
    /* Prefer the translated text for a Chinese UI when present. */
    const _useTranslated = !!(rec.translatedContent
      && typeof convAutoTranslate === 'function' && conv && convAutoTranslate(conv));
    const _body = _useTranslated ? rec.translatedContent : (rec.content || '');
    const _bodyHtml = (typeof renderMarkdown === 'function')
      ? renderMarkdown(_body) : escapeHtml(_body);

    const overlay = document.createElement('div');
    overlay.id = 'apReportModal';
    overlay.className = 'ap-report-overlay';
    overlay.setAttribute('data-ap-report-run', runId);
    overlay.innerHTML =
      `<div class="ap-report-modal" role="dialog" aria-modal="true">`
      + `<div class="ap-report-head">`
      + `<svg class="aps-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>`
      + `<span class="ap-report-title">${escapeHtml(_title)}</span>`
      + `<span class="ap-report-private">${escapeHtml(_privNote)}</span>`
      + `<button class="ap-report-close" aria-label="Close" title="Close">`
      + `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`
      + `</button>`
      + `</div>`
      + `<div class="ap-report-body md-content">${_bodyHtml}</div>`
      + `</div>`;

    const close = () => {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('.ap-report-close').addEventListener('click', close);
    document.addEventListener('keydown', onKey);

    document.body.appendChild(overlay);
  } catch (e) {
    console.warn('[Autopilot report modal] open failed (non-fatal):', e && e.message);
  }
}
if (typeof window !== 'undefined') window._openApSummaryModal = _openApSummaryModal;

/* Click handler for the "Summarize this run" button on a manual-stop run's
 * concluding VU turn. Reached from renderMessage's action bar (the run-fold
 * that used to carry it was deleted in the 2026-07-07 flatten). */
async function _summarizeAutopilotRun(runId, btn) {
  const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
  if (!conv) return;
  const _restore = () => {
    if (btn) {
      btn.disabled = false;
      btn.textContent = (typeof t === 'function' ? t('autopilot.summarizeRun') : 'Summarize this run');
    }
  };
  if (btn) { btn.disabled = true; btn.textContent = (typeof t === 'function' ? t('common.loading') : 'Loading…'); }
  let cfg = {};
  try {
    if (typeof _buildConvConfig === 'function') cfg = await _buildConvConfig(conv);
  } catch (e) {
    console.warn('[Autopilot] summarize: _buildConvConfig failed, using defaults:', e && e.message);
  }
  try {
    const res = await Api.chat.summarizeAutopilotRun(conv.id, runId, cfg);
    if (res && res.ok && res.summary && res.summary.content) {
      /* ★ The summary is a human-only SIDECAR record (NOT a chat message).
       * Store it on conv.autopilotSummaries so the fold renders its report
       * panel — never push it into conv.messages. */
      if (!conv.autopilotSummaries || typeof conv.autopilotSummaries !== 'object') {
        conv.autopilotSummaries = {};
      }
      conv.autopilotSummaries[res.runId || runId] = res.summary;
      if (typeof saveConversations === 'function') saveConversations(conv.id);
      try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); } catch (e) { /* non-fatal */ }
      if (typeof renderChat === 'function') renderChat(conv, true);
    } else {
      _restore();
    }
  } catch (e) {
    console.warn('[Autopilot] summarize run failed:', e && e.message);
    _restore();
  }
}
if (typeof window !== 'undefined') window._summarizeAutopilotRun = _summarizeAutopilotRun;

/* Is THIS VU turn the run's CONCLUDING turn — i.e. the newest message in the
 * conversation stamped with this run id? A run's report affordance belongs on
 * exactly one turn: its last VU turn. Because only VU turns carry
 * `_autopilotRunId` (never the agent follow-ups) and a NEWER run / a real user
 * message carries a DIFFERENT (or no) run id, "newest turn with this runId" is
 * the correct owner even when the run was superseded — no boundary-walk needed.
 * Returns false when the run has no surviving turn in the loaded window (e.g.
 * after compaction) so the caller renders nothing; see the known-gap note in
 * _apRunReportAffordance. */
function _isRunConcludingTurn(conv, msg) {
  const runId = msg && msg._autopilotRunId;
  if (!conv || !runId || !Array.isArray(conv.messages)) return false;
  let lastIdx = -1;
  for (let i = 0; i < conv.messages.length; i++) {
    const m = conv.messages[i];
    if (m && m._autopilotRunId === runId) lastIdx = i;
  }
  if (lastIdx === -1) return false;
  return conv.messages[lastIdx] === msg;
}

/* Build the run-close-out affordance button for a VU turn's action bar, or ''.
 * ONLY on the run's concluding turn (newest turn with the run id), and only
 * once the backend wrote the run's concluded record:
 *   • report present (clean [VU: TASK_DONE]) → "View report" → _openApSummaryModal.
 *   • concluded, reason='stopped', no report (manual stop) → "Summarize this
 *     run" → _summarizeAutopilotRun.
 * A running / un-concluded run gets nothing. Inline SVG glyphs per §3.4.
 * KNOWN GAP (flagged, not silently dropped): if a run's report exists in
 * conv.autopilotSummaries[runId] but NONE of its VU turns survive in the loaded
 * message window (e.g. compaction dropped them), there is no turn to hang this
 * on and the report is currently unreachable from the transcript. The record is
 * NOT lost (still in settings.autopilotSummaries) — a future run-index surface
 * could reach it; today that path is out of scope. */
function _apRunReportAffordance(conv, msg) {
  if (!msg || !msg._isVirtualUser || !msg._autopilotRunId) return '';
  if (!_isRunConcludingTurn(conv, msg)) return '';
  const runId = msg._autopilotRunId;
  const rec = _apRunRecord(conv, runId);
  if (!rec || rec.status !== 'concluded') return '';
  const _tOr = (k, fb) => (typeof t === 'function' && t(k) !== k) ? t(k) : fb;
  const _rid = escapeHtml(runId);
  if (rec.content) {
    const label = _tOr('autopilot.viewReport', 'View report');
    return `<button class="msg-action-btn ap-report-btn" onclick="event.stopPropagation();_openApSummaryModal('${_rid}')" title="${escapeHtml(label)}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg> ${escapeHtml(label)}</button>`;
  }
  if (rec.reason === 'stopped') {
    const label = _tOr('autopilot.summarizeRun', 'Summarize this run');
    return `<button class="msg-action-btn ap-summarize-btn" onclick="event.stopPropagation();_summarizeAutopilotRun('${_rid}',this)" title="${escapeHtml(label)}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M12 18v-6"/><path d="M9 15h6"/></svg> ${escapeHtml(label)}</button>`;
  }
  return '';
}

/* ★ ALWAYS-VISIBLE inline run-summary PANEL (owner directive 2026-07-08).
 * The 2026-07-07 flatten deleted the `autopilot-run-fold` <details> that used
 * to auto-render a concluded run's close-out report; the report was left
 * reachable ONLY via a hover "View report" button → functionally invisible.
 * This builds an open-by-default collapsible card, rendered INLINE below the
 * run's concluding turn by renderMessage — no hover, no click required. The
 * body reuses the SAME markdown + translated-preference rendering as the modal
 * (`_openApSummaryModal`); the header carries a small "expand" button that
 * opens that modal for a focused full-screen read of a long report. The panel
 * is human-only — never a chat message, never sent to the agent.
 * Returns '' when the run has no report content (a bare manual-stop record — a
 * `status:'concluded'` with no `content` — keeps only the "Summarize this run"
 * action-bar affordance). */
function _apReportPanelHTML(conv, runId) {
  const rec = _apRunSummary(conv, runId);   // content-present records only
  if (!rec) return '';
  const _tOr = (k, fb) => (typeof t === 'function' && t(k) !== k) ? t(k) : fb;
  const title = _tOr('autopilot.summaryLabel', 'Run summary report');
  const priv = _tOr('autopilot.summaryHumanOnly', 'For you only · not sent to the agent');
  /* Prefer the translated debrief for a Chinese UI when present — identical
   * selection to _openApSummaryModal so the panel and the modal agree. */
  const _useTranslated = !!(rec.translatedContent
    && typeof convAutoTranslate === 'function' && conv && convAutoTranslate(conv));
  const bodyText = _useTranslated ? rec.translatedContent : (rec.content || '');
  const bodyHtml = (typeof renderMarkdown === 'function')
    ? renderMarkdown(bodyText) : escapeHtml(bodyText);
  const _rid = escapeHtml(runId);
  let incompleteH = '';
  if (rec.incomplete) {
    const _needs = _tOr('autopilot.needsReview', 'stopped early · needs review');
    incompleteH = `<span class="aps-incomplete"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg> ${escapeHtml(_needs)}</span>`;
  }
  const expandTitle = _tOr('autopilot.viewReport', 'View report');
  return `<details class="ap-summary-panel" open data-ap-report-run="${_rid}">`
    + `<summary class="aps-summary">`
    + `<svg class="aps-doc-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>`
    + `<span class="aps-title">${escapeHtml(title)}</span>`
    + incompleteH
    + `<span class="aps-private">${escapeHtml(priv)}</span>`
    + `<button class="aps-expand-btn" onclick="event.preventDefault();event.stopPropagation();_openApSummaryModal('${_rid}')" title="${escapeHtml(expandTitle)}"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg></button>`
    + `<svg class="aps-toggle" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`
    + `</summary>`
    + `<div class="aps-body md-content">${bodyHtml}</div>`
    + `</details>`;
}

/* Where each concluded-run REPORT docks in the transcript. A report is a
 * SIDECAR fact (conv.autopilotSummaries[runId]), NOT a messages[] entry, so it
 * has no natural row — it is rendered as a STANDALONE boundary node by the
 * post-render pass `_applyAutopilotSummaryPanels`, never grafted onto a
 * neighbouring bubble. This resolver decides the dock point for each run that
 * has report content:
 *   • { runId, afterMsgIdx } — the run has surviving turns in the loaded
 *     window: dock the panel at the run's TRUE BOUNDARY, i.e. AFTER the LAST
 *     message stamped with this run id (the concluding VU turn — its follow-up
 *     agent reply belongs to the NEXT turn/run, so the last stamped turn is the
 *     boundary). This replaces the old graft-under-the-user-bubble anchor.
 *   • { runId, tail: true } — COMPACTION/lazy-window fallback: the run's turns
 *     did NOT survive the loaded window, so there is no boundary to dock at.
 *     Render at the conversation tail (oldest orphan first) so the report is
 *     never unreachable.
 * Deterministic order: boundary docks by afterMsgIdx ascending, then all tail
 * orphans by run ts ascending. */
function _apSummaryPlacements(conv) {
  if (!conv || !conv.autopilotSummaries || typeof conv.autopilotSummaries !== 'object') return [];
  const msgs = Array.isArray(conv.messages) ? conv.messages : [];
  // Last message index stamped with each run id (its boundary).
  const lastIdxByRun = new Map();
  for (let i = 0; i < msgs.length; i++) {
    const rid = msgs[i] && msgs[i]._autopilotRunId;
    if (rid) lastIdxByRun.set(rid, i);
  }
  const boundary = [];
  const tail = [];
  for (const runId of Object.keys(conv.autopilotSummaries)) {
    if (!_apRunSummary(conv, runId)) continue;   // only reports with content
    if (lastIdxByRun.has(runId)) {
      boundary.push({ runId, afterMsgIdx: lastIdxByRun.get(runId) });
    } else {
      tail.push({ runId, tail: true, ts: (conv.autopilotSummaries[runId] || {}).ts || 0 });
    }
  }
  boundary.sort((a, b) => a.afterMsgIdx - b.afterMsgIdx);
  tail.sort((a, b) => a.ts - b.ts);   // oldest orphan first
  return boundary.concat(tail);
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
  if (Array.isArray(msg.toolRounds)) {
    for (const r of msg.toolRounds) {
      if (r.compactionLayer) compactedCount++;
      compactedToSum += (r.compactedToChars | 0);
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
  /* NOTE: the autopilot run-summary REPORT is deliberately NOT folded into this
   * per-message fingerprint. It is not owned by any message — it renders as a
   * standalone boundary node via the post-render `_applyAutopilotSummaryPanels`
   * pass, and a background-sync arrival is caught by the conv-level Guard 2
   * fingerprint (`_apSummariesFp` in core.js), which triggers renderChat →
   * the panel pass re-docks it. Keeping it out of `_msgFingerprint` avoids
   * repainting the neighbouring bubble just because a sidecar report arrived. */
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
  return (msg.role || "") + ":" +
    (msg.content || "").length + ":" +
    (msg.thinking || "").length + ":" +
    _errFp + ":" +
    (msg.finishReason || "") + ":" +
    (msg.translatedContent || "").length + ":" +
    (msg._showingTranslation ? "T" : "F") + ":" +
    (msg._translateDone === false ? "P" : "") + ":" +
    (sr ? sr.length : 0) + ":" +
    (msg._igResult ? "IG" : "") + ":" +
    (msg._igResults ? msg._igResults.length : 0) + ":" +
    (msg._igError ? "IGE" : "") + ":" +
    (msg.modifiedFiles || 0) + ":" +
    (msg.images ? msg.images.length : 0) + ":" +
    (msg.pdfTexts ? msg.pdfTexts.length : 0) + ":" +
    (msg._autopilotRunId || "") + ":" +
    (msg._isAutopilotSummary ? "S" : "") + ":" +
    "c" + compactedCount + "ts" + compactedToSum +
    (swarmFp ? ":sw" + swarmFp : "");
}

/* ── Tool-round freshness gradient ──
 *
 * The server compaction cliff (lib/tasks_pkg/compaction.py:MICRO_HOT_TAIL=40)
 * is correct for performance (most runs <40 tool calls) but invisible to
 * the user.  A binary hot/cold cutoff also reads as "broken UI" in the
 * common case where everything is hot.
 *
 * Better signal: a continuous fade of every tool row by its distance
 * from the newest round.  Newest = 100 % opacity, gradually receding
 * toward an older floor.  The user sees AT A GLANCE that recent rounds
 * are bright and older ones recede — the same intuition the model has,
 * surfaced visually.  No magic numbers exposed in the UI; the cliff
 * still exists server-side, but the user sees a smooth gradient.
 *
 * Position-based (NOT length-normalized) so the same round in the same
 * place always renders the same way regardless of how long the conv
 * grows: position 0 from end = 1.00, pos 30 = 0.70, pos 80+ = 0.55 floor.
 *
 * Compacted rows (`compactionLayer` set) skip the fade — they have
 * their own purple/pink stripe and shouldn't compete with the gradient. */
/* Age-based fading was REMOVED per UX directive: transparency is too
 * quiet, and the thing the user actually needs to see — compaction
 * state — is now expressed with a SOLID, opaque label per row (see
 * compactionLabel logic in _renderUnifiedToolLine).
 *
 * Kept callable from existing render entry points so callers don't NPE. */
function _stampFreshness(_conv) { /* no-op */ }

/* ── Background data-refresh repaint (scroll-preserving) ──────────────────
 * Cost + file-change batch prefetches (and their per-message async fallback)
 * land data that `_msgFingerprint` deliberately does NOT track, so the
 * surgical diff in renderChat would treat every message as "unchanged" and
 * skip the repaint. Historically these callbacks worked around that by
 * calling `renderChat(conv, true)` — the FULL re-render path, which (a) resets
 * the lazy window back to the last _INITIAL_RENDER messages (dropping any
 * older messages a scrolled-up reader force-loaded), (b) flashes the whole
 * list via the `cv-off` height recompute, and (c) ends in
 * `_forceScrollToBottom()`, yanking the reader to the bottom. Firing ~1s after
 * a conversation opens (when the batch responses arrive) — precisely while the
 * user is scrolling up to read history — that is the reported "flicker back
 * and forth then jump to the bottom" bug.
 *
 * This repaints the CURRENTLY-RENDERED assistant messages in place. Only
 * `role==='assistant'` bubbles carry the cost / file-changes / finish bars, so
 * user bubbles (and their quote/ref badges) are left untouched. It upholds two
 * invariants so a data-only refresh is invisible to the reader:
 *
 *   (1) COMPARE-BEFORE-SWAP. Each assistant bubble is `outerHTML`-replaced ONLY
 *       when its freshly-rendered HTML differs from what we last rendered for
 *       it (stamped as `__bgHtml` on the node). Unchanged bubbles keep their
 *       exact DOM node — preserving any manually-expanded tool-round `<details>`
 *       state and skipping needless layout. Same principle as the `data-mfp`
 *       surgical diff, extended to the async cost/file-change data that
 *       `_msgFingerprint` deliberately omits. If NOTHING changed we return
 *       before touching the DOM or scroll at all.
 *
 *   (2) ANCHOR-RELATIVE SCROLL RESTORE. A raw `scrollTop = saved` restore is
 *       only correct when heights ABOVE the fold don't change — but that is
 *       exactly what changes here: on first open the bars aren't fetched yet,
 *       so above-fold assistant bubbles render short and GROW when the batch
 *       lands. Restoring the raw pixel then drifts the reader downward by the
 *       total added height above the fold. Instead we pin the topmost message
 *       element intersecting the viewport: record its offset from the container
 *       top BEFORE the swaps, then after the swaps adjust `scrollTop` so that
 *       same element sits at the same visual offset. The reader's viewport is
 *       preserved even when above-fold bubbles change height.
 *
 * Both measurements happen under the same `cv-off` guard `_renderMsgInPlace`
 * uses, so the swap can't collapse `content-visibility:auto` intrinsic-size
 * caches and mis-resolve the geometry. No-op during streaming (the live bubble
 * is owned by the streaming UI) or before any message is laid out. */
/* ── Shared anchor-relative scroll preservation ──────────────────────────
 * Capture the topmost message element still intersecting the viewport and its
 * offset from the container top; after a DOM mutation that may change heights
 * (including ABOVE the fold), re-pin that element to the same visual offset.
 * This is the same technique proven in `_bgRefreshChat`, extracted so the
 * full-render path (which used to reset scrollTop→0 via innerHTML wipe) can
 * reuse it verbatim instead of relying on `_forceScrollToBottom`. Callers wrap
 * the swap in the `cv-off` guard so `content-visibility:auto` intrinsic-size
 * caches can't collapse and mis-resolve the geometry. */
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

function _bgRefreshChat(conv) {
  if (!conv || conv.id !== activeConvId) return;
  if (activeStreams.has(conv.id) && document.getElementById('streaming-msg')) return;
  const inner = document.getElementById('chatInner');
  const container = document.getElementById('chatContainer');
  if (!inner || !container) return;
  const els = inner.querySelectorAll('[id^="msg-"]');
  if (!els.length) return;  // welcome / loading skeleton — nothing to repaint

  const total = conv.messages.length;

  /* ── (1) Compare-before-swap: build the swap list, skipping bubbles whose
   *        rendered output is byte-identical to what we last painted. ── */
  const updates = [];
  els.forEach((el) => {
    const m = el.id.match(/^msg-(\d+)$/);
    if (!m) return;
    const idx = parseInt(m[1], 10);
    if (idx >= total) return;
    const msg = conv.messages[idx];
    if (!msg || msg.role !== 'assistant') return;
    const fresh = renderMessage(msg, idx);
    if (el.__bgHtml === fresh) return;  // unchanged → keep DOM (expand state intact)
    updates.push({ idx, el, html: fresh });
  });
  if (!updates.length) return;  // nothing changed → reader untouched

  /* ── (2) Anchor-relative scroll preservation across the swaps. ── */
  inner.classList.add('cv-off');
  void inner.scrollHeight;  // force real heights before measuring
  const anchor = _captureScrollAnchor(container, inner);

  for (const u of updates) {
    u.el.outerHTML = u.html;
    const n = document.getElementById('msg-' + u.idx);
    if (n) n.__bgHtml = u.html;  // stamp so a later no-change refresh skips it
  }

  void inner.scrollHeight;  // re-layout after swaps
  _restoreScrollAnchor(container, anchor);
  inner.classList.remove('cv-off');
  if (!activeStreams.has(conv.id)) {
    _applyAutopilotRunFolds(inner, conv);
    _applyAutopilotSummaryPanels(inner, conv);
  }
  _lastRenderedFingerprint = _convRenderFingerprint(conv);
}

function renderChat(conv, forceScroll) {
  /* ── Guard 1: skip if user is editing a message in this conversation ── */
  if (_editingMsgIdx !== null && conv.id === activeConvId) return;
  /* Stamp freshness on every render so the panel chrome always reflects
   * the current cross-message position of each round.  Cheap pass —
   * one assignment per round, no DOM work. */
  _stampFreshness(conv);
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
  if (forceScroll === false && conv.id === activeConvId && conv.messages.length > 0 && _hasMsgDom && !conv._initialSwitchLoad) {
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
    for (let i = startIdx; i < total; i++) {
      if (i === _skipIdx) continue;  // streaming message — leave #streaming-msg alone
      const msg = conv.messages[i];
      const el = document.getElementById("msg-" + i);
      if (el) {
        /* Element exists — check if content changed */
        const oldFp = el.getAttribute("data-mfp") || "";
        const newFp = _msgFingerprint(msg);
        if (oldFp !== newFp) {
          _pendingUpdates.push({ el, html: renderMessage(msg, i) });
          anyChange = true;
        }
      } else {
        /* New message — append */
        const wrapper = document.createElement("div");
        wrapper.innerHTML = renderMessage(msg, i);
        const newEl = wrapper.firstElementChild;
        if (newEl) inner.appendChild(newEl);
        anyChange = true;
      }
    }
    /* Apply all outerHTML replacements in a single write batch */
    for (const upd of _pendingUpdates) {
      upd.el.outerHTML = upd.html;
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
      _applyAutopilotSummaryPanels(inner, conv);
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
    _applyAutopilotSummaryPanels(inner, conv);
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
  } else {
    _forceScrollToBottom(container, true);
    /* ★ Latch this open so subsequent same-open full renders (Phase-2
     *   reconcile, .then() fallback) preserve the viewport instead of
     *   re-snapping to the bottom. Only meaningful while _initialSwitchLoad
     *   is set; loadConversation clears both flags when the open completes. */
    if (conv._initialSwitchLoad) _openScrollConvId = conv.id;
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

function renderMessage(msg, idx) {
  /* Belt: drop an orphaned empty-assistant placeholder (see
   * _isOrphanEmptyAssistant). Returning '' produces no DOM node — the surgical
   * update path replaces via outerHTML='' (removes it), the append path skips a
   * null firstElementChild, and the full render contributes nothing. */
  if (_isOrphanEmptyAssistant(msg)) return "";
  const isUser = msg.role === "user" || msg.role === "optimizer";  // optimizer = endpoint review, render as user
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
      body += `<div class="pdf-attach-badge" title="${escapeHtml(pdf.name)}" onclick="previewMsgPdfText(${idx},${pdfI})" style="cursor:pointer"><span class="pdf-attach-icon">${docIcon}</span><span class="pdf-attach-info"><span class="pdf-attach-name">${escapeHtml(pdf.name.length > 25 ? pdf.name.slice(0, 23) + "…" : pdf.name)}</span><span class="pdf-attach-meta">${pdf.pages} pages · ${sizeStr}${imgStr}${scanBadge}${methodBadge}</span></span></div>`;
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
        body += `<div class="reply-quote-badge conv-ref-badge" title="引用对话: ${crTitle}">
          <span class="reply-quote-badge-icon">@</span>
          <span class="reply-quote-badge-info">
            <span class="reply-quote-badge-name">${crTitle}</span>
            <span class="reply-quote-badge-meta">引用对话</span>
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
   *   When the flag is ON and this finished message carries a `segments` list,
   *   render tools with their PRECEDING thinking+narration inline (per-tool),
   *   instead of the three grouped blocks.
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
  if (_segTimelineAllowed && _segTimelineEnabled()
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
  if (msg.thinking && !_segTimelineRendered) {
    const thinkLen = msg.thinking.length;
    const thinkMeta = thinkLen >= 1024 ? ` (${Math.round(thinkLen / 1024)}k chars)` : ` (${thinkLen} chars)`;
    body += `<div class="thinking-block" onclick="_toggleThinking(this,${idx})"><div class="thinking-header"><span class="thinking-label">Thinking Process${thinkMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
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
    body += `<div class="thinking-block thinking-prior" onclick="_togglePriorThinking(this,${idx})"><div class="thinking-header"><span class="thinking-label">Earlier Thinking${priorMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
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
    body += `<div class="thinking-block thinking-prior content-prior" onclick="_togglePriorContent(this,${idx})"><div class="thinking-header"><span class="thinking-label">Earlier Response${priorCMeta} · rolled back</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
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
      const bannerLabel = distinctModels > 1 ? `全模型 ${results.length}连抽` : `${results.length}连抽`;
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">${bannerLabel} · ${okResults.length}/${results.length} 成功</div><div class="ig-batch-grid ig-cols-${cols}">`;
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
            <button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(${idx},${ri},${promptEsc},${modelEsc})" title="Retry this slot">↻ Retry</button>
          </div></div>`;
        }
      }
      body += `</div></div>`;
    }
  } else if (msg.content) {
    try {
      let mdHtml;
      // Show translated content only when translation is active (not toggled off).
      // ★ Endpoint-mode critic messages are role=user but produced by the
      //   Critic LLM — they DO receive server-side auto-translate.  Treat
      //   them like assistants for the purposes of translation display.
      // ★ Virtual-user (Autopilot) messages are role=user but produced by the
      //   VU LLM and auto-translated to the UI language for DISPLAY — treat
      //   them like critic/assistant for translation display (the VU composes
      //   in the assistant's language; the user sees it in their UI language).
      const _isCritic = isUser && (msg._isEndpointReview || msg._isVirtualUser);
      const showTrans = (!isUser || _isCritic)
                        && msg.translatedContent
                        && msg._showingTranslation !== false;
      if (showTrans) {
        mdHtml = renderMarkdown(stripNoTranslateTags(msg.translatedContent));
      } else if (_isCritic) {
        // Critic / VU messages are user-role but contain rich markdown
        mdHtml = renderMarkdown(msg.content);
      } else if (isUser) {
        mdHtml = escapeHtml(stripNoTranslateTags(msg.originalContent || msg.content));
      } else {
        mdHtml = renderMarkdown(msg.content);
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
        ? _zapSvg + ' 紧急压缩'
        : (trig === 'manual' ? _wrenchSvg + ' 手动压缩' : _archiveSvg + ' 自动压缩');
      const before = c.tokensBefore || 0;
      const after  = c.tokensAfter  || 0;
      const reductionPct = (c.reductionPct != null) ? c.reductionPct
                          : (before > 0 ? Math.round((1 - after / before) * 100) : 0);
      const sizeFrag = before > 0 && after > 0
        ? `${(before/1000).toFixed(0)}k → ${(after/1000).toFixed(0)}k tokens · -${reductionPct}%`
        : (before > 0 ? `${(before/1000).toFixed(0)}k tokens 已归档` : '已归档');
      const reasonFrag = c.reason ? `<span class="compaction-marker-reason">${escapeHtml(c.reason)}</span>` : '';
      const statusCls = (c.status === 'done') ? 'is-done' : 'is-progress';
      const archiveAttr = (c.archiveId == null) ? '' : `data-archive-id="${c.archiveId}"`;
      const convAttr = `data-conv-id="${escapeHtml(c.convId || '')}"`;
      return `<button type="button" class="compaction-marker ${statusCls}"
        ${archiveAttr} ${convAttr}
        onclick="if(window.openCompactionViewer){window.openCompactionViewer(this.dataset.convId, parseInt(this.dataset.archiveId,10))}else{console.warn('compaction-viewer not loaded')}"
        title="点击查看压缩前的完整上下文">
        <span class="compaction-marker-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg></span>
        <span class="compaction-marker-trigger">${trigLabel}</span>
        <span class="compaction-marker-stat">${escapeHtml(sizeFrag)}</span>
        ${reasonFrag}
        <span class="compaction-marker-cta">查看历史</span>
      </button>`;
    }).join('');
    body += `<div class="compaction-marker-row">${_ccDom}</div>`;
  }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${escapeHtml(msg.content)}</div>`;
    }
  }
  // ── Bilingual display ──
  if (isUser && msg.originalContent && msg.originalContent !== msg.content) {
    const _tmUser = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    // Strip any leaked <notranslate>/<nt> tags from the translation display.
    const _userTrans = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-translated"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type active">译文</span>${_tmUser}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'user',${idx})" title="Copy translation"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content user-content">${escapeHtml(_userTrans)}</div></div></div>`;
  }
  // ── Auto-translate failed notice (user messages) ──
  // The send-path auto-translate was attempted but failed / timed out, so the
  // ORIGINAL (untranslated) text was sent to the model. We surface a quiet,
  // non-blocking notice instead of silently dropping the translation — click
  // to retranslate just this message.
  if (isUser && !msg._isEndpointReview && !msg.originalContent && msg._translateFailed) {
    const _failKind = msg._translateFailed === 'timed_out' ? 'timed_out' : 'failed';
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
    body += `<div class="translate-failed-notice" onclick="event.stopPropagation();regenerateFromUser(${idx})" title="${escapeHtml(_failMsg)}">`
      + `<svg class="tfn-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`
      + `<span class="tfn-text">${escapeHtml(_failMsg)}</span>`
      + `<span class="tfn-retry">${escapeHtml(_retryTip)}</span>`
      + `</div>`;
  }
  if (!isUser && msg.translatedContent && msg._showingTranslation !== false) {
    const _tmAsst = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    // Defense in depth — strip any leaked <notranslate>/<nt> tags.
    const _asstOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type">译文</span>${_tmAsst}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'assistant',${idx})" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_asstOrig)}</div></div></div>`;
  }
  // ── Critic (endpoint review) / Autopilot VU bilingual block — symmetric with assistant ──
  if (isUser && (msg._isEndpointReview || msg._isVirtualUser) && msg.translatedContent && msg._showingTranslation !== false) {
    const _tmCritic = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    const _critOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type">译文</span>${_tmCritic}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'critic',${idx})" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_critOrig)}</div></div></div>`;
  }
  // ── Persistent "translating..." indicator (survives re-render / tab switch) ──
  // Fires for both assistant messages AND endpoint-critic (role=user,
  // _isEndpointReview) messages — both are routed through the auto-translate
  // pipeline.
  if ((!isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser)))
      && !msg.translatedContent && msg._translateDone === false) {
    const errText = msg._translateError;
    if (errText) {
      body += `<div class="translate-loading" id="translate-loading-${idx}" style="color:#f59e0b;cursor:pointer" onclick="translateMessage(${idx})">${t('translate.failed')}</div>`;
    } else {
      // ── Show a retry-status sub-line when the backend is retrying
      //    (e.g. 429 / rate-limit / empty-output). Without this the user
      //    sees only "Translating…" and has no idea there's a problem. ──
      let statusSub = '';
      const _benignKinds = (typeof _TRANSLATE_BENIGN_STATUS_KINDS !== 'undefined')
        ? _TRANSLATE_BENIGN_STATUS_KINDS : new Set(['started', 'in_progress']);
      if (msg._translateStatus && !_benignKinds.has(msg._translateStatusKind || '')) {
        const kind = msg._translateStatusKind || '';
        // Prefer a localized label keyed by kind, fall back to the raw server message.
        const i18nKey = kind ? `translate.retry.${kind}` : '';
        const localized = i18nKey && typeof t === 'function' ? t(i18nKey) : '';
        const display = (localized && localized !== i18nKey) ? localized : msg._translateStatus;
        statusSub = `<div class="translate-status-sub" title="${escapeHtml(msg._translateStatus)}">⚠ ${escapeHtml(display)}</div>`;
      }
      // ── Streaming preview: render the partial translation as markdown as it
      //    arrives, in a styled block that morphs smoothly into the final
      //    bilingual译文 once the stream completes. ──
      // ★ Segment-timeline (step 5b): when the interleaved per-tool timeline is
      //   active for THIS turn, the per-round translated narration is already
      //   shown INLINE (settled `_renderTimelineBatch` reads seg.translatedText;
      //   streaming `_renderStreamingTranslatePreview` paints per-round Chinese),
      //   and the answer shows as the bilingual block above. The standalone
      //   bottom `.translate-preview` blob then merely DUPLICATES that inline
      //   text — and is exactly the block that "reappears on pause / mid-stream
      //   re-render", fighting the interleaved layout. Suppress it here (keep
      //   the quiet spinner head so the user still sees translation is running)
      //   and stamp `data-seg-timeline` so the in-place poll patch
      //   (`_patchTranslateLoadingDom`) also skips re-creating it.
      let previewSub = '';
      if (msg._translatePartial && !_segTimelineRendered) {
        let _pv;
        try { _pv = renderMarkdown(stripNoTranslateTags(msg._translatePartial)); }
        catch (e) { _pv = escapeHtml(msg._translatePartial); }
        previewSub = `<div class="translate-preview"><div class="md-content">${_pv}</div><span class="translate-caret"></span></div>`;
      }
      const _hasPreview = previewSub ? ' has-preview' : '';
      const _segTlAttr = _segTimelineRendered ? ' data-seg-timeline="1"' : '';
      body += `<div class="translate-loading${_hasPreview}" id="translate-loading-${idx}"${_segTlAttr}>`
        + `<div class="translate-loading-head"><span class="translate-spinner"></span>`
        + `<span class="translate-loading-label">${t('translate.translatingToCN')}</span></div>`
        + `${statusSub}${previewSub}</div>`;
    }
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
    const copyH = `<button class="msg-action-btn copy-msg-btn" onclick="event.stopPropagation();copyMessage(${idx})" title="Copy"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy</button>`;
    /* ★ Edit / Regen are USER-LANE actions, but an autopilot VU turn is
     *   role=user + MACHINE-authored (`_isVirtualUser`). Editing a synthetic
     *   "keep going" driver turn and regenerating from it is nonsensical AND
     *   lets a user inject a hand-edited driver message mid-autopilot — so the
     *   VU row gets the SHARED actions (Copy / Translate / Delete) but NOT
     *   Edit / Regen. `_isVirtualUser` is its own carve-out here; a real human
     *   user turn (incl. a role=user peer-message turn) still shows both. */
    const _humanAuthored = isUser && !msg._isVirtualUser;
    /* Edit is available on EVERY bubble now (user, agent, autopilot VU,
     * critic, planner). For a real human user turn it opens the full editor
     * (Save / Save & Resend); for every other lane it is EDIT-IN-PLACE only
     * (Save) — startEditMessage / saveEditOnly branch on the role. Regen
     * stays human-only (see _humanAuthored below). */
    const editH = `<button class="msg-action-btn" onclick="event.stopPropagation();startEditMessage(${idx})" title="Edit"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit</button>`;
    const regenH = _humanAuthored
      ? `<button class="msg-action-btn msg-regen-btn" onclick="event.stopPropagation();regenerateFromUser(${idx})" title="Regenerate response from this message"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Regen</button>`
      : "";
    const conv_ = getActiveConv();
    const isLastAssistant =
      !isUser &&
      conv_ &&
      idx === conv_.messages.length - 1 &&
      !activeStreams.has(conv_.id);
    const continueH = isLastAssistant
      ? `<button class="msg-action-btn msg-continue-btn" onclick="event.stopPropagation();continueAssistant()" title="Continue generating from where it left off"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Continue</button>`
      : "";
    const isShowingTrans = msg._showingTranslation;
    // Show the Translate button on: (a) assistant messages, (b) endpoint
    // critic review messages (role=user + _isEndpointReview) — they
    // receive auto-translate too.
    const _translateBtnAllowed = !isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser));
    const translateH = _translateBtnAllowed
      ? `<button class="msg-action-btn msg-translate-btn${isShowingTrans ? " translated" : ""}" onclick="event.stopPropagation();translateMessage(${idx})" title="${isShowingTrans ? "Show Original" : "Translate"}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0014.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg> ${isShowingTrans ? "Original" : "Translate"}</button>`
      : "";
    const exportImgH = !isUser
      ? `<button class="msg-action-btn msg-export-img-btn" onclick="event.stopPropagation();ExportImages.exportMessageWithPreview(${idx})" title="Export as phone-screen images"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg> Export</button>`
      : "";
    const canDelete = conv_ && !activeStreams.has(conv_.id) && !conv_.activeTaskId;
    const deleteH = canDelete
      ? `<button class="msg-action-btn msg-delete-btn" onclick="event.stopPropagation();deleteTurn(${idx})" title="${isUser ? 'Delete this turn' : 'Delete this message'}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>`
      : "";
    const apReportH = _apRunReportAffordance(conv_, msg);
    actionBtns = `<div class="message-actions">${copyH}${editH}${regenH}${continueH}${translateH}${apReportH}${exportImgH}${deleteH}</div>`;
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
  const mfpAttr = typeof idx === "number" ? raw(` data-mfp="${_msgFingerprint(msg)}"`) : "";
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
  /* Final assembly via safeHtml. roleName / userLabel / time are
   * escaped by default. Everything pre-built above (avatars = trusted
   * SVG, badgeHtml, body, branchHtml, actionBtns, the class/attr
   * fragments) is already-trusted HTML, marked raw(). */
  const _classAttr = `${isUser ? ' user-msg' : ''}${msg._isEndpointReview ? ' ep-critic-msg' : ''}${epPlannerCls}${epWorkerCls}${vuCls}`;
  return String(safeHtml`<div class="message${raw(_classAttr)}"${raw(idAttr)}${raw(msgIdAttr)}${raw(apRunAttr)}${raw(apSummaryAttr)}${raw(mfpAttr)}><div class="message-avatar">${raw(isUser ? userAvatar : avatarContent)}</div><div class="message-content"><div class="message-header"><span class="message-role">${isUser ? userLabel : roleName}</span>${raw(badgeHtml)}${raw(messageTimeHtml)}</div><div class="message-body">${raw(body)}</div>${raw(branchHtml)}${raw(actionBtns)}</div>${raw(turnCtxHtml)}</div>`);
}
