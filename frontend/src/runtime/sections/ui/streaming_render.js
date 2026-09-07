/* ===== migrated source: ui/streaming_render.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   streaming render — extracted from ui.js (split 2026-05-28)

   Autopilot disarm responses reconciled into backend-authored run metadata.
   Live transcript projection belongs exclusively to the typed Turn store.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Apply a BACKEND-AUTHORITATIVE autopilot run-concluded record onto a conv.
 *
 * ONE record per run (`{runId, status:'concluded', reason:'task_done'|
 * 'stopped', content?, translatedContent?, ts, _summaryId}`) carries BOTH the
 * terminal fold-fact AND the optional close-out report (a manual stop has no
 * `content`). It is human-only: stored backend-side under
 * `settings.autopilotSummaries[runId]`, mirrored here onto
 * `conv.autopilotSummaries[runId]`, never into the catalog shell.
 * (transcript) nor the LLM context. Idempotent + monotonic: a re-delivery
 * (reconnect / cold-replay / settings round-trip) overwrites the same runId
 * entry, but a bare `stopped` record NEVER clobbers an existing `task_done`
 * record that already carries a report.
 *
 * The disarm response returns this record so an idle disarm — no live stream
 * — folds instantly without a reload. Returns true iff a record was applied.
 */
function _applyAutopilotRunConcluded(conv, rec, runId) {
  if (!conv || !rec || typeof rec !== 'object') return false;
  runId = runId || rec.runId;
  if (!runId) return false;
  if (!conv.autopilotSummaries || typeof conv.autopilotSummaries !== 'object') {
    conv.autopilotSummaries = {};
  }
  const prior = conv.autopilotSummaries[runId];
  /* Monotonic merge: a manual `stopped` record must not erase an earlier
   * clean `task_done` record's report/verdict (they can race on close-out). */
  const priorIsCleanReport = !!(prior && prior.reason === 'task_done' && prior.content);
  const incomingIsBareStop = (rec.reason === 'stopped') && !rec.content;
  if (priorIsCleanReport && incomingIsBareStop) return false;
  const _reason = rec.reason || (prior && prior.reason) || 'task_done';
  conv.autopilotSummaries[runId] = {
    runId,
    status: rec.status || 'concluded',
    reason: _reason,
    content: rec.content || (prior && prior.content) || '',
    translatedContent: rec.translatedContent || (prior && prior.translatedContent) || '',
    ts: rec.ts || Date.now(),
    _summaryId: rec._summaryId || (prior && prior._summaryId) || '',
    /* Preserve the "stopped early — needs review" flag. A clean task_done
     * supersedes an incomplete stop (reason no-downgrade), so drop the flag
     * when the merged reason is task_done. */
    incomplete: (_reason !== 'task_done')
      && !!(rec.incomplete || (prior && prior.incomplete)),
    /* UNSENT — the `content` is a VU reply that was PRODUCED but never
     * delivered into the conversation (the run yielded to a human / was
     * superseded mid-flight). MUST survive this merge: it is the only thing
     * that lets the UI say "this was written but never sent" instead of
     * presenting it as a turn that happened. Dropping it here would make the
     * backend field dead on arrival — the "declared but never rendered"
     * failure this fix exists to end. Cleared by a clean task_done, same as
     * `incomplete`. */
    unsent: (_reason !== 'task_done')
      && !!(rec.unsent || (prior && prior.unsent)),
  };
  return true;
}

/**
 * Apply a disarm response's ``runConcluded`` record to the conv and re-render.
 *
 * The disarm endpoint (toggle-OFF / queue-cancel) is the manual-stop arm of
 * the conclude contract: it returns the SAME backend-authoritative record the
 * SSE ``autopilot_run_concluded`` event carries. Because a disarm can happen
 * when there is NO live SSE stream (the reply already finished — the idle case)
 * the client would otherwise never receive the concluded fact until a reload;
 * applying the response body here makes the run fold instantly. No-op when the
 * response carried no record (nothing was an autopilot run to conclude).
 */
function _applyDisarmResponse(convId, resp) {
  try {
    const rec = resp && resp.runConcluded;
    if (!rec) return;
    const conv = conversations.find(c => c.id === convId);
    if (!conv) return;
    if (!_applyAutopilotRunConcluded(conv, rec, rec.runId)) return;
    reconcileConversationCatalogMetadata(convId);
    try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    if (activeConvId === convId) {
      runtimeScope.requestAuthoritativeConversationRender(
        convId, { forceScroll: true },
      );
    }
  } catch (e) {
    console.warn('[Autopilot] apply disarm response failed:', e && e.message);
  }
}
