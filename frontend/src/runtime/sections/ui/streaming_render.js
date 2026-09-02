/* ===== migrated source: ui/streaming_render.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   streaming render — extracted from ui.js (split 2026-05-28)

   Autopilot virtual-user events projected as typed transient Turns.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Browser-only identity registry for Autopilot's transient virtual-user turns.
 * Content is owned by the typed reducer and transient Turn overlay.
 */
const _autopilotVuTransientTurns = new Map();

function _autopilotVuRegistryKey(convId, vuMsgId) {
  return String(convId || '') + ':' + String(vuMsgId || '');
}
function _findVuMsgById(conv, vuMsgId) {
  if (!conv || !vuMsgId) return null;
  const key = _autopilotVuRegistryKey(conv.id, vuMsgId);
  const registered = _autopilotVuTransientTurns.get(key);
  const turnId = registered?.turnId || autopilotVuTransientTurnId(vuMsgId);
  const turn = runtimeScope.ConversationTransientTurns?.get?.(conv.id, turnId);
  if (!turn) {
    if (registered) _autopilotVuTransientTurns.delete(key);
    return null;
  }
  return { turn, msg: turn.projection, idx: -1 };
}

function _showAutopilotVuTransient(conv, vuMsgId, turn, previousTurnId) {
  if (!conv || !turn) return null;
  const service = runtimeScope.ConversationTransientTurns;
  if (!service) return null;
  if (previousTurnId && previousTurnId !== turn.turnId && service.replace) {
    service.replace(conv, previousTurnId, turn);
  } else {
    service.upsert?.(conv, turn);
  }
  _autopilotVuTransientTurns.set(
    _autopilotVuRegistryKey(conv.id, vuMsgId),
    { turnId: turn.turnId, startedAt: turn.createdAt || Date.now() },
  );
  return { turn, msg: turn.projection, idx: -1 };
}

function _beginVuStreaming(convId, conv, vuMsgId, replaySnapshot) {
  return _showAutopilotVuTransient(
    conv,
    vuMsgId,
    createAutopilotVuTransientTurn({
      conversationId: convId,
      vuMsgId,
      runId: vuMsgId,
      replaySnapshot: replaySnapshot || undefined,
    }),
  );
}

function _flushVuStreaming(convId, vuMsgId) {
  const conv = conversations.find((item) => item.id === convId);
  const entry = conv ? _findVuMsgById(conv, vuMsgId) : null;
  if (!conv || !entry) return false;
  return Boolean(runtimeScope.ConversationTransientTurns?.upsert?.(conv, entry.turn));
}

function _maskVuMachineTokens(text) {
  return maskAutopilotVuMachineTokens(String(text || ''));
}
if (typeof window !== 'undefined') {
  runtimeScope._maskVuMachineTokens = _maskVuMachineTokens;
}

function _autopilotVuInnerIsVisible(inner) {
  const type = inner?.type || '';
  return type === 'tool_start'
    || type === 'phase'
    || (type === 'delta' && Boolean(inner.content || inner.thinking));
}

function _removeAutopilotVuTransient(conv, vuMsgId) {
  const key = _autopilotVuRegistryKey(conv.id, vuMsgId);
  const entry = _findVuMsgById(conv, vuMsgId);
  if (!entry) {
    _autopilotVuTransientTurns.delete(key);
    return false;
  }
  const removed = runtimeScope.ConversationTransientTurns?.remove?.(
    conv, entry.turn.turnId,
  ) || false;
  _autopilotVuTransientTurns.delete(key);
  return Boolean(removed);
}

function _adoptSettledAutopilotVuTurn(conv, vuMsgId, current, finalMessage) {
  const terminal = settleAutopilotVuTransientTurn(
    current?.turn || null,
    conv.id,
    vuMsgId,
    finalMessage,
  );
  _showAutopilotVuTransient(
    conv,
    vuMsgId,
    terminal,
    current?.turn?.turnId || '',
  );

  const service = runtimeScope.ConversationTurnStore;
  if (!service?.hydrateConversation) {
    console.warn(
      '[Autopilot VU] settled overlay retained because TurnStore hydration is unavailable',
    );
    return;
  }
  Promise.resolve(service.hydrateConversation(conv)).then(() => {
    runtimeScope.ConversationTransientTurns?.remove?.(conv, terminal.turnId);
    _autopilotVuTransientTurns.delete(
      _autopilotVuRegistryKey(conv.id, vuMsgId),
    );
  }).catch((error) => {
    /* Keep the backend-authored terminal mirror visible. A later snapshot or
     * conversation reopen will replace it; nothing local is persisted. */
    _autopilotVuTransientTurns.delete(
      _autopilotVuRegistryKey(conv.id, vuMsgId),
    );
    console.warn(
      '[Autopilot VU] authoritative hydration failed after settle:',
      error?.message || error,
    );
  });
}

/**
 * Fold Autopilot VU lifecycle frames into one immutable transient Turn.
 *
 * Durable completion always comes from the backend: vu_done is shown only as
 * a terminal mirror while TurnStore hydration converges, then removed.
 */
function _handleAutopilotVuEvent(convId, ev) {
  const conv = conversations.find((item) => item.id === convId);
  if (!conv) {
    console.debug(
      '[Autopilot VU] conv=' + convId.slice(0, 8)
      + ' not found; dropping ' + String(ev?.type || ''),
    );
    return;
  }
  const vuMsgId = ev?.vuMsgId;
  if (!vuMsgId) {
    console.warn('[Autopilot VU] missing vuMsgId on lifecycle event', ev);
    return;
  }

  if (ev.type === 'autopilot_vu_cancel') {
    _removeAutopilotVuTransient(conv, vuMsgId);
    return;
  }

  const current = _findVuMsgById(conv, vuMsgId);
  if (ev.type === 'autopilot_vu_done') {
    _adoptSettledAutopilotVuTurn(conv, vuMsgId, current, ev.vuMessage || {});
    return;
  }

  if (ev.type === 'autopilot_vu_start') {
    if (!current) {
      _beginVuStreaming(
        convId,
        conv,
        vuMsgId,
        ev.replaySnapshot && typeof ev.replaySnapshot === 'object'
          ? ev.replaySnapshot : null,
      );
      return;
    }
    if (ev.replaySnapshot && typeof ev.replaySnapshot === 'object') {
      const next = reduceAutopilotVuTransientTurn(current.turn, ev);
      _showAutopilotVuTransient(conv, vuMsgId, next);
    }
    return;
  }

  if (ev.type !== 'autopilot_vu_event') return;
  const inner = ev.inner || {};
  let live = current;
  if (!live) {
    if (!_autopilotVuInnerIsVisible(inner)) return;
    live = _beginVuStreaming(convId, conv, vuMsgId, null);
  }
  if (!live) return;
  const next = reduceAutopilotVuTransientTurn(live.turn, ev);
  if (next !== live.turn) {
    _showAutopilotVuTransient(conv, vuMsgId, next);
  }
}

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
 * Shared by the SSE `autopilot_run_concluded` handler AND the disarm response
 * (which returns the same record so an idle disarm — no live stream — folds
 * instantly without a reload). Returns true iff a record was applied.
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
 * Handle the `autopilot_run_concluded` SSE event: the single BACKEND fact that
 * an autopilot run reached its terminal boundary — a clean [VU: TASK_DONE]
 * (reason=task_done, with a report) OR a manual stop (reason=stopped, no
 * report). Receiving it is what lets `_applyAutopilotRunFolds` fold the run
 * (the gate keys on `conv.autopilotSummaries[runId].status==='concluded'` —
 * see `_apRunConcluded`); the report, when present, renders as the fold's
 * read-only PANEL. The record is human-only — never a chat message.
 *
 * Tolerates the legacy shape (`ev.summary`/`ev.summaryMessage`) during rollout.
 */
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
    if (typeof saveConversations === 'function') saveConversations(convId);
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

function _handleAutopilotRunConcluded(convId, ev) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.debug(`[Autopilot run] conv=${convId.slice(0,8)} not found — dropping`);
    return;
  }
  /* New shape: `record`. Legacy rollout shapes: `summary` / `summaryMessage`. */
  const rec = ev.record || ev.summary || ev.summaryMessage;
  const runId = ev.runId || (rec && rec.runId);
  if (!rec || !runId) {
    console.warn('[Autopilot run] missing concluded record / runId', ev);
    return;
  }
  if (!_applyAutopilotRunConcluded(conv, rec, runId)) return;
  const _stored = conv.autopilotSummaries[runId] || {};
  console.info(
    `[Autopilot run] ✓ run=${(runId||'').slice(0,12)} concluded ` +
    `(reason=${_stored.reason}, ${(_stored.content||'').length} report chars, ` +
    `NOT a message) for conv=${convId.slice(0,8)}`
  );
  if (typeof saveConversations === "function") saveConversations(convId);
  try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
  catch (e) { /* non-fatal */ }
  if (activeConvId === convId) {
    runtimeScope.requestAuthoritativeConversationRender(
      convId, { forceScroll: true },
    );
  }
}
