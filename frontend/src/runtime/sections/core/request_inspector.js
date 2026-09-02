/* ===== migrated source: core/request_inspector.js ===== */
/* Request Inspector adapter: task events are authoritative; debug-panel owns
 * the detail renderer. Opens by stable task/round identity. */

let _riOpen = false;
const _riSel = { taskId: null, fold: null };
const _riTaskRows = {};

function toggleRequestInspector() {
  if (_riOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riOpen = true;
  _riSel.taskId = null;
  _riSel.fold = null;
  /* Keep the legacy debugVisible flag in sync — other readers (restore
   * paths, the _applyDebugModeVisibility helper) key off it. */
  if (typeof debugVisible !== 'undefined') debugVisible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riResetDetail();
  _riLoadTasks(typeof activeConvId !== 'undefined' ? activeConvId : null);
}

function closeRequestInspector() {
  _riOpen = false;
  if (typeof debugVisible !== 'undefined') debugVisible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(String(taskId));
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riOpen) return;
  _riSel.taskId = null;
  _riSel.fold = null;
  _riResetDetail();
  _riLoadTasks(convId);
}

/* Turn badge label for a request row (P4): Flow node phases read through
 * i18n; swarm agents show their role; anything else falls back to the raw
 * tag so a future turn value still renders. */
function _riTurnLabel(row) {
  if (row.turn === 'swarm-agent') return row.agentRole || 'agent';
  const key = 'ri.turn' + String(row.turn || '').charAt(0).toUpperCase() +
    String(row.turn || '').slice(1);
  const v = t(key);
  return v === key ? String(row.turn) : v;
}

function _riEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(s == null ? '' : String(s)) : String(s == null ? '' : s);
}

function _riTimeLabel(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch (_) { return ''; }
}

function _riEl(id) { return document.getElementById(id); }

function _riSetDetailActive(active) {
  const drawer = _riEl('riDrawer');
  if (drawer) drawer.classList.toggle('ri-detail-active', !!active);
}

function _riResetDetail() {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.detailTitle');
  if (content) content.innerHTML = `<div class="ri-main-empty">` +
    `${_riEsc(t('ri.selectRound'))}</div>`;
  _riSetDetailActive(false);
}

function _riStatusInfo(row) {
  const raw = String((row && row.status) || '').toLowerCase();
  if ((row && row.live) || ['running', 'queued', 'pending'].includes(raw))
    return { tone: 'running', label: t('ri.statusRunning') };
  if (['done', 'completed', 'complete', 'success'].includes(raw))
    return { tone: 'done', label: t('ri.statusDone') };
  if (['error', 'failed', 'failure'].includes(raw))
    return { tone: 'error', label: t('ri.statusFailed') };
  if (['aborted', 'interrupted', 'cancelled', 'canceled', 'stopped'].includes(raw))
    return { tone: 'stopped', label: t('ri.statusStopped') };
  return { tone: 'neutral', label: raw || t('ri.statusUnknown') };
}

function _riRevealTechnical() {
  const details = _riEl('riRoundList') &&
    _riEl('riRoundList').querySelector('.ri-technical');
  if (details) details.open = true;
}

function _riSummaryCard(taskId, taskRow, fold) {
  const status = _riStatusInfo(taskRow || {});
  const operationCount = Number(fold && fold.operationCount);
  const hasOperationCount = !!fold && fold.eventsAvailable !== false &&
    fold.operationCountAvailable !== false &&
    Object.prototype.hasOwnProperty.call(fold, 'operationCount') &&
    Number.isFinite(operationCount) && operationCount >= 0;
  let operationLabel = t('ri.operationUnavailable');
  if (hasOperationCount) {
    if (operationCount > 0) {
      const key = fold.operationCountApproximate
        ? (operationCount === 1
          ? 'ri.operationCountApproxOne' : 'ri.operationCountApprox')
        : (operationCount === 1 ? 'ri.operationCountOne' : 'ri.operationCount');
      operationLabel = t(key, { n: operationCount });
    } else operationLabel = t('ri.noOperations');
  }
  const el = document.createElement('section');
  el.className = 'ri-summary';
  el.setAttribute('aria-label', t('ri.summaryLabel'));
  el.innerHTML =
    `<div class="ri-summary-status ri-tone-${_riEsc(status.tone)}">` +
    `<span class="ri-summary-dot" aria-hidden="true"></span>` +
    `<span>${_riEsc(status.label)}</span></div>` +
    `<div class="ri-summary-count">${_riEsc(operationLabel)}</div>` +
    `<div class="ri-summary-help">${_riEsc(t('ri.operationHelp'))}</div>` +
    `<div class="ri-summary-task">${_riEsc(t('ri.taskLabel', {
      id: String(taskId || '').slice(0, 8) }))}</div>`;
  return el;
}

/* ── Level 1: task rows for the active conversation ── */
async function _riLoadTasks(convId) {
  const list = _riEl('riTaskList');
  const rounds = _riEl('riRoundList');
  if (!list) return;
  if (rounds) rounds.innerHTML = '';
  for (const key of Object.keys(_riTaskRows)) delete _riTaskRows[key];
  if (!convId) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(convId) : null;
  if (!_riOpen) return;  // drawer closed while fetching
  const tasks = (data && Array.isArray(data.tasks)) ? data.tasks : [];
  if (!tasks.length) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  if (rounds && !_riSel.taskId) {
    rounds.innerHTML = `<div class="ri-empty ri-select-task">` +
      `${_riEsc(t('ri.selectTask'))}</div>`;
  }
  list.innerHTML = '';
  for (const row of tasks) {
    _riTaskRows[row.taskId] = row;
    const el = document.createElement('div');
    el.className = 'ri-task' + (_riSel.taskId === row.taskId ? ' ri-sel' : '') +
      (row.isSwarmAgent ? ' ri-task-agent' : '');
    el.dataset.taskId = row.taskId;
    const status = _riStatusInfo(row);
    const expired = !row.hasEvents && !row.live;
    const agentBadge = row.isSwarmAgent
      ? `<span class="ri-agent-badge">${_riEsc(row.agentId || 'agent')}</span> · ` : '';
    el.innerHTML =
      `<div class="ri-task-top">` +
      `<span class="ri-task-status ri-tone-${_riEsc(status.tone)}">` +
      `<span class="ri-task-status-dot" aria-hidden="true"></span>` +
      `${_riEsc(status.label)}</span>` +
      `<span class="ri-task-time">${_riEsc(_riTimeLabel(row.createdAt))}</span>` +
      `</div>` +
      `<div class="ri-task-sub">` +
      agentBadge +
      (expired
        ? `<span class="ri-expired">${_riEsc(t('ri.expired'))}</span>`
        : `<span>${_riEsc(t('ri.viewProcess'))}</span>`) +
      `<span class="ri-task-id">${_riEsc(t('ri.taskLabel', {
        id: String(row.taskId).slice(0, 8) }))}</span>` +
      `</div>`;
    el.onclick = () => _riSelectTask(row.taskId);
    list.appendChild(el);
  }
}

/* ── Level 2: request rows (metadata) for the selected task ── */
async function _riSelectTask(taskId) {
  _riSel.taskId = taskId;
  _riSel.fold = null;
  _riSel.traceOpen = false;
  _riResetDetail();
  /* Re-mark the selected task row. */
  const list = _riEl('riTaskList');
  if (list) {
    list.querySelectorAll('.ri-task').forEach((el) => {
      el.classList.toggle('ri-sel', el.dataset.taskId === taskId);
    });
  }
  const rounds = _riEl('riRoundList');
  if (!rounds) return;
  rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  const fold = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequests(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale response
  _riSel.fold = fold;
  rounds.innerHTML = '';
  if (!fold || !fold.eventsAvailable) {
    rounds.appendChild(_riSummaryCard(taskId, _riTaskRows[taskId], fold));
    const expired = document.createElement('div');
    expired.className = 'ri-empty';
    expired.textContent = t('ri.expired');
    rounds.appendChild(expired);
    return;
  }
  rounds.appendChild(_riSummaryCard(taskId, _riTaskRows[taskId], fold));
  /* Turn Trace entry (耗时分析): sits between the summary and the
   * technical details — the ONE click that answers "where did the time
   * go" for this task, folded server-side (docs/TURN_TRACE_CONTRACT.md). */
  const traceEntry = document.createElement('div');
  traceEntry.className = 'ri-trace-entry';
  traceEntry.setAttribute('role', 'button');
  traceEntry.innerHTML =
    `<span class="ri-trace-label">${_riEsc(t('ri.traceEntry'))}</span>` +
    `<span class="ri-trace-hint">${_riEsc(t('ri.traceEntryHint'))}</span>`;
  traceEntry.onclick = () => _riOpenTrace(taskId);
  rounds.appendChild(traceEntry);
  const technical = document.createElement('details');
  technical.className = 'ri-technical';
  const technicalHead = document.createElement('summary');
  technicalHead.innerHTML = `<span>${_riEsc(t('ri.technicalDetails'))}</span>` +
    `<span class="ri-technical-count">${_riEsc(t('ri.modelTurns', {
      n: fold.requestCount || 0 }))}</span>`;
  technical.appendChild(technicalHead);
  const technicalBody = document.createElement('div');
  technicalBody.className = 'ri-technical-body';
  technical.appendChild(technicalBody);
  rounds.appendChild(technical);
  /* Coverage chip — honest disclosure (design §7). 'flow-untagged':
   * a Flow log whose planner/worker/critic rounds exist but
   * share numbers with no phase tag (ambiguous, NOT uncovered). */
  if (fold.coverage === 'partial') {
    const reasonKey = fold.coverageReason === 'flow-untagged'
      ? 'ri.coverageAmbiguous' : 'ri.coveragePartial';
    const chip = document.createElement('div');
    chip.className = 'ri-coverage-chip';
    chip.innerHTML =
      (typeof Icon === 'function' ? Icon('alertTriangle', 12) : '') +
      ` <span>${_riEsc(t(reasonKey))}</span>`;
    technicalBody.appendChild(chip);
  }
  const reqs = Array.isArray(fold.requests) ? fold.requests : [];
  if (!reqs.length) {
    const emp = document.createElement('div');
    emp.className = 'ri-empty';
    emp.textContent = t('ri.empty');
    technicalBody.appendChild(emp);
  }
  for (const row of reqs) {
    const el = document.createElement('div');
    el.className = 'ri-round';
    el.dataset.round = String(row.roundNum);
    el.dataset.turn = row.turn || '';
    const attempts = Array.isArray(row.attempts) ? row.attempts : [];
    const tok = row.approxTokens >= 1000
      ? (row.approxTokens / 1000).toFixed(1) + 'K' : String(row.approxTokens || 0);
    const attemptBits = attempts.map((a) => {
      const el2 = (a.streamElapsedMs / 1000).toFixed(1) + 's';
      const fb = /FALLBACK|REACTIVE|DISCARDED/.test(a.tag || '') ? ' ⚠' : '';
      return `<span class="ri-attempt" title="${_riEsc(a.traceId || '')}">` +
        `${_riEsc(a.tag || a.model)} ${a.tokensIn}→${a.tokensOut} · ${el2}${fb}</span>`;
    }).join('');
    const visibleToolCount = row.wireToolsCount != null
      ? row.wireToolsCount : row.toolsCount;
    const turnBadge = row.turn
      ? `<span class="ri-turn-badge">${_riEsc(_riTurnLabel(row))}</span>` : '';
    el.innerHTML =
      `<div class="ri-round-top">` +
      turnBadge +
      `<span class="ri-round-n">${_riEsc(t('ri.roundNumber', {
        n: row.roundNum }))}</span>` +
      `<span class="ri-round-model">${_riEsc(row.model || '?')}</span>` +
      `<span class="ri-round-meta">${_riEsc(t('ri.roundMeta', {
        messages: row.messageCount, tokens: tok }))}` +
      (visibleToolCount ? ` · ${_riEsc(t('ri.availableTools', {
        n: visibleToolCount }))}` : '') + `</span>` +
      `</div>` +
      (attemptBits ? `<div class="ri-round-attempts">${attemptBits}</div>` : '');
    el.onclick = () => _riSelectRound(taskId, row.roundNum, el, row.turn || '');
    technicalBody.appendChild(el);
  }
  /* State mirrors (NOT requests) — collapsed at the bottom, clearly labeled. */
  const states = Array.isArray(fold.states) ? fold.states : [];
  if (states.length) {
    const head = document.createElement('div');
    head.className = 'ri-states-head';
    head.textContent = `${t('ri.states')} (${states.length}) — ${t('ri.stateNote')}`;
    technicalBody.appendChild(head);
    for (const s of states) {
      const el = document.createElement('div');
      el.className = 'ri-state-row';
      el.setAttribute('role', 'button');
      el.tabIndex = 0;
      el.title = t('ri.stateRowTip');
      el.textContent = `${s.label || s.roundNum} · ${s.messageCount} msgs`;
      /* State rows are NAVIGATION (the drawer's quick-jump list): open the
       * state mirror INLINE next to the tool call that produced it; falls
       * back to the drawer detail when the tool row isn't in the DOM. */
      el.onclick = () => openStateInspector(taskId, s.roundNum);
      technicalBody.appendChild(el);
    }
  }
}

function _riShowWireProjection(projection) {
  if (!projection || !Array.isArray(projection.toolNames)) return;
  const content = _riEl('debugContent');
  if (!content) return;
  const details = document.createElement('details');
  details.className = 'ri-coverage-chip ri-wire-projection';
  const summary = document.createElement('summary');
  const bits = [
    t('ri.availableTools', { n: projection.toolCount || 0 }),
    t('ri.wireSchemaTokens', { n: projection.schemaTokens || 0 }),
  ];
  if (projection.backend) bits.push(projection.backend);
  if (projection.schemaBudgetTokens) {
    bits.push(t('ri.wireBudget', { n: projection.schemaBudgetTokens }));
  }
  if (Array.isArray(projection.budgetDroppedNames)
      && projection.budgetDroppedNames.length) {
    bits.push(t('ri.wireDropped', {
      n: projection.budgetDroppedNames.length,
    }));
  }
  summary.textContent = bits.join(' · ');
  const names = document.createElement('pre');
  names.textContent = projection.toolNames.join('\n');
  details.append(summary, names);
  content.prepend(details);
}

/* ── Level 3: detail — REUSES showMessagesInDebug (no second renderer) ── */

/* Bounded payload cache; live SSE snapshots win over server reads. */
const _riPayloadCache = {};
const _RI_PAYLOAD_CACHE_MAX = 40;
async function _riFetchPayload(taskId, roundNum, turn, kind) {
  turn = turn || '';
  kind = kind || 'request';
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Prefer live snapshots; Flow phases are keyed by turn|roundNum. */
  const _acc = (typeof _debugRequests !== 'undefined') && _debugRequests[taskId];
  if (kind === 'state') {
    const st = _acc && (_acc.states || []).filter((s) => s && s.messages &&
      String(s.roundNum) === String(roundNum)).pop();
    if (st) {
      const payload = { messages: st.messages, tools: st.tools,
        label: st.label, model: st.model, params: st.params, kind: 'state' };
      _riPayloadCache[key] = payload;
      return payload;
    }
  } else {
    const _accKey = turn ? turn + '|' + roundNum : String(roundNum);
    const acc = _acc && _acc.rounds[_accKey];
    if (acc && acc.messages) {
      const payload = { messages: acc.messages, tools: acc.tools,
        label: acc.label, model: acc.model, params: acc.params,
        turn: acc.turn || turn };
      _riPayloadCache[key] = payload;
      return payload;
    }
  }
  if (_riPayloadCache[key]) return _riPayloadCache[key];
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequestPayload(taskId, roundNum, turn || undefined,
        kind === 'state' ? 'state' : undefined) : null;
  if (data && data.messages) {
    const ids = Object.keys(_riPayloadCache);
    if (ids.length >= _RI_PAYLOAD_CACHE_MAX) delete _riPayloadCache[ids[0]];
    _riPayloadCache[key] = data;
    return data;
  }
  return null;
}

/* Longest exact JSON prefix; divergence safely produces no fold. */
function _riSharedPrefix(prevMsgs, curMsgs) {
  const n = Math.min(prevMsgs.length, curMsgs.length);
  let k = 0;
  while (k < n && JSON.stringify(prevMsgs[k]) === JSON.stringify(curMsgs[k])) k++;
  return k;
}

/* Return only messages appended by this round; null selects the tail fallback. */
async function _riRoundScopedMessages(taskId, roundNum, kind, messages) {
  const num = parseInt(roundNum, 10);
  if (!Number.isFinite(num) || num <= 1 || !Array.isArray(messages)) return null;
  const prev = await _riFetchPayload(taskId, num - 1, '',
    kind === 'state' ? 'state' : 'request');
  if (!prev || !Array.isArray(prev.messages) || !prev.messages.length)
    return null;
  const k = _riSharedPrefix(prev.messages, messages);
  return k > 0 ? messages.slice(k) : null;
}

/* Tail fallback excludes system/history and keeps this round's tool exchange. */
function _riTailSlice(messages) {
  if (!Array.isArray(messages)) return [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'assistant' &&
        Array.isArray(m.tool_calls) && m.tool_calls.length)
      return messages.slice(i);
  }
  /* Final-answer round: use content after the last user message. */
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i] && messages[i].role === 'user') {
      const tail = messages.slice(i + 1);
      if (tail.length) return tail;
      break;
    }
  }
  /* Last resort: show non-system messages. */
  return messages.filter((m) => m && m.role !== 'system');
}

async function _riSelectRound(taskId, roundNum, el, turn) {
  turn = turn || '';
  _riSel.traceOpen = false;
  _riRevealTechnical();
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.toggle('ri-sel', r === el));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.remove('ri-sel'));
  }
  const payload = await _riFetchPayload(taskId, roundNum, turn);
  // Stale: the user moved on — including into the Turn Trace view, which a
  // late round-payload resolve must not clobber (the reverse race is
  // already guarded in _riOpenTrace).
  if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;
  if (!payload || !payload.messages) return;
  /* Fold against the previous round in the same Flow phase. */
  let opts = { resetScroll: true };
  const num = parseInt(roundNum, 10);
  if (Number.isFinite(num) && num > 1) {
    const prev = await _riFetchPayload(taskId, num - 1, turn);
    if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;  // stale
    if (prev && prev.messages) {
      const k = _riSharedPrefix(prev.messages, payload.messages);
      if (k > 0) { opts.foldPrefix = k; opts.diffBase = 'R' + (num - 1); }
    }
  }
  if (typeof showMessagesInDebug === 'function') {
    _riSetDetailActive(true);
    showMessagesInDebug(payload.messages, payload.label || '', false,
      typeof activeConvId !== 'undefined' ? activeConvId : null,
      payload.tools || undefined, false, undefined,
      Object.assign(opts, { contextManifest: payload.contextManifest || [] }));
    _riShowWireProjection(payload.wireProjection);
  }
}

/* Resolve a tool row's producing task from its stable Turn/attempt identity. */
function _riTaskIdForRound(round) {
  try {
    if (round?._taskId) return String(round._taskId);
    const turnId = String(round?._turnId || '');
    const state = runtimeScope.ConversationTurnRead?.state?.(activeConvId);
    const turn = turnId ? state?.turnsById?.[turnId] : null;
    if (!turn || turn.actor === 'virtual_user') return '';
    const attempts = Object.values(state.attemptsById || {}).filter(
      (attempt) => attempt?.turnId === turnId && attempt.taskId,
    );
    attempts.sort((left, right) => Number(right.createdAt || 0) - Number(left.createdAt || 0));
    return attempts[0]?.taskId || '';
  } catch (e) {
    console.warn('[ri] taskId-for-round resolve failed:', e);
  }
  return '';
}

/* Open the request that produced a tool call. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  if (!taskId || roundNum == null) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(taskId);
  _riRevealTechnical();
  const fold = _riSel.fold;
  const reqs = (fold && Array.isArray(fold.requests)) ? fold.requests : [];
  /* Same-numbered Flow phases prefer the worker request. */
  const exact = reqs.filter((r) => String(r.roundNum) === String(roundNum));
  const pick = exact.find((r) => r.turn === 'working') || exact[0] ||
    reqs[reqs.length - 1];
  if (!pick) return;
  const targetTurn = pick.turn || '';
  const el = document.querySelector(
    '#riRoundList .ri-round[data-round="' + String(pick.roundNum) +
    '"][data-turn="' + targetTurn + '"]') ||
    document.querySelector(
      '#riRoundList .ri-round[data-round="' + String(pick.roundNum) + '"]');
  if (el) {
    if (typeof el.scrollIntoView === 'function')
      el.scrollIntoView({ block: 'nearest' });
    el.classList.add('ri-flash');
    setTimeout(() => el.classList.remove('ri-flash'), 1600);
  }
  await _riSelectRound(taskId, pick.roundNum, el, targetTurn);
}

/* ── Tool-row debug panel (ONE view: the post-tool result state) ──────────
 * ONE entry per tool row opening ONE view. The former request | state tab
 * pair was redundant, not two questions: the state mirror for round N is
 * captured AFTER the tool results are appended to the SAME message list the
 * request was built from (lib/tasks_pkg/tool_dispatch/_pipeline.py — same
 * roundNum axis), so the request payload is a strict PREFIX of the mirror.
 * Showing both meant clicking twice to see the same messages minus the
 * results (owner, 2026-07-29: "we don't need both").
 *
 * The request axis survives as a FALLBACK, not a tab: swarm sub-agents
 * persist kind='request' snapshots only (lib/swarm/agent.py has no state
 * emission), so a state-only panel would render "mirror missing" on every
 * sub-agent tool row. `_riFetchRoundView` therefore tries the mirror first
 * and degrades to the request, telling the caller which one it got so the
 * chip can name the axis instead of silently mislabelling it.
 *
 * Mounts right after the tool round's [data-prn] slot and renders through
 * the SAME renderer as the drawer detail (renderDebugBlocksInto — no second
 * JSON renderer). When the tool row is not in the DOM (unloaded/old
 * conversation), degrades to the drawer so the click always lands somewhere
 * meaningful.
 *
 * ROUND-SCOPED (owner, 2026-07-28): renders ONLY what that round appended —
 * the increment over the previous round's same-kind payload — never the full
 * conversation-history dump ("records only for this round of tool calls are
 * sufficient"). The cross-round chip strip was removed with it: one click
 * answers one round; the drawer remains the place for cross-round
 * navigation. */
async function openToolDebugPanel(taskId, roundNum, anchorEl) {
  if (!taskId || roundNum == null) return;
  let slot = (anchorEl && typeof anchorEl.closest === 'function')
    ? anchorEl.closest('[data-prn]') : null;
  if (!slot) {
    const marker = document.querySelector(
      '[data-ri-state="' + String(taskId) + ':' + String(roundNum) + '"]');
    if (marker && typeof marker.closest === 'function')
      slot = marker.closest('[data-prn]');
  }
  if (!slot) {
    /* Tool row not in the DOM (unloaded / old conversation) — degrade to the
     * drawer instead of a dead click, showing the SAME view the inline panel
     * would have: the result state, or the request when no mirror exists. */
    if (!_riOpen) openRequestInspector();
    await _riSelectTask(taskId);
    const view = await _riFetchRoundView(taskId, roundNum);
    if (view && view.payload && view.payload.messages &&
        typeof showMessagesInDebug === 'function') {
      _riSetDetailActive(true);
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        typeof activeConvId !== 'undefined' ? activeConvId : null,
        view.payload.tools || undefined, false, undefined,
        { resetScroll: true,
          contextManifest: view.payload.contextManifest || [] });
      return;
    }
    await openRequestInspectorForToolRound(taskId, roundNum);
    return;
  }
  /* Re-clicking the entry for the round already open closes it (toggle). */
  const existing = document.querySelector('.ri-state-panel');
  if (existing && existing.dataset.riRound === String(roundNum) &&
      existing.dataset.riTask === String(taskId)) {
    existing.remove();
    return;
  }
  _riMountToolPanel(slot, taskId, roundNum);
}

/* Back-compat entry: the drawer's state list addresses a round's state
 * mirror directly, which is what the panel now shows outright. */
async function openStateInspector(taskId, roundNum, anchorEl) {
  return openToolDebugPanel(taskId, roundNum, anchorEl);
}

/* Resolve the ONE view a tool row's debug entry shows, and say which axis it
 * came from. The post-tool mirror is preferred because it is a superset of
 * the request; the request is the fallback for rounds that never emitted a
 * mirror (swarm sub-agents, an aborted round, an expired state row).
 * Returns {kind, payload} or null when neither axis has anything. */
async function _riFetchRoundView(taskId, roundNum) {
  const state = await _riFetchPayload(taskId, roundNum, '', 'state');
  if (state && state.messages && state.messages.length)
    return { kind: 'state', payload: state };
  const req = await _riFetchPayload(taskId, roundNum, '', 'request');
  if (req && req.messages && req.messages.length)
    return { kind: 'request', payload: req };
  return null;
}

/* Mount the (single-instance) panel after a tool slot. Transient by
 * design — a chat re-render may drop it; re-click reopens. */
async function _riMountToolPanel(slot, taskId, roundNum) {
  document.querySelectorAll('.ri-state-panel').forEach((p) => p.remove());
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  panel.dataset.riTask = String(taskId);
  panel.dataset.riRound = String(roundNum);
  panel.innerHTML =
    '<div class="ri-state-panel-head">' +
      '<span class="ri-state-panel-kind"></span>' +
      '<span class="ri-state-panel-title"></span>' +
      '<span class="ri-state-panel-close" role="button" tabindex="0" title="' +
        _riEsc(t('ri.stateClose')) + '">' +
        (typeof Icon === 'function' ? Icon('x', 12) : '') + '</span>' +
    '</div>' +
    '<div class="ri-state-body"><div class="ri-empty">' +
      _riEsc(t('ri.loading')) + '</div></div>';
  panel.querySelector('.ri-state-panel-close').onclick = () => panel.remove();
  slot.insertAdjacentElement('afterend', panel);
  if (typeof panel.scrollIntoView === 'function')
    panel.scrollIntoView({ block: 'nearest' });
  await _riRenderToolPanel(panel, taskId, roundNum);
}

/* Render the panel's ONE view: the message mirror captured right after this
 * round's tools ran, or the producing request when that round emitted no
 * mirror. Goes through the shared debug renderer, scoped to this round's
 * increment. The kind chip names the axis on screen, so a fallback render is
 * never mistaken for the mirror. */
async function _riRenderToolPanel(panel, taskId, roundNum) {
  if (!panel.isConnected) return;  // closed while fetching
  panel.dataset.riRound = String(roundNum);
  panel.dataset.riPanel = taskId + ':' + roundNum;
  const body = panel.querySelector('.ri-state-body');
  const titleEl = panel.querySelector('.ri-state-panel-title');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  const view = await _riFetchRoundView(taskId, roundNum);
  if (!panel.isConnected) return;
  if (!view) {
    panel.dataset.riKind = '';
    if (kindEl) kindEl.textContent = '';
    if (titleEl) titleEl.textContent = 'R' + roundNum;
    if (body) body.innerHTML = '<div class="ri-empty">' +
      _riEsc(t('ri.stateEmpty')) + '</div>';
    return;
  }
  const payload = view.payload;
  panel.dataset.riKind = view.kind;
  if (kindEl) {
    kindEl.textContent = t(view.kind === 'state'
      ? 'ri.tabState' : 'ri.tabRequest');
    kindEl.classList.toggle('ri-kind-fallback', view.kind !== 'state');
    kindEl.title = t(view.kind === 'state'
      ? 'ri.stateKindTip' : 'ri.requestKindTip');
  }
  /* Round-scoped: only what THIS round appended (see the section header).
   * When no exact increment exists, a state mirror degrades to its TAIL
   * slice (the tool call + its results) — never the full payload, whose
   * system prompt + history is precisely what this panel must not dump.
   * The request axis (the mirror-less fallback) keeps the full payload: it
   * has no post-tool tail to slice. */
  const scoped = await _riRoundScopedMessages(taskId, roundNum, view.kind,
    payload.messages);
  if (!panel.isConnected) return;
  const shown = (Array.isArray(scoped) && scoped.length)
    ? scoped
    : (view.kind === 'state'
      ? _riTailSlice(payload.messages) : payload.messages);
  if (titleEl) titleEl.textContent =
    (payload.label || ('R' + roundNum)) + ' · +' + shown.length + ' msgs';
  if (body) {
    renderDebugBlocksInto(body, shown, null);
    /* 2026-08-05 owner: NO tools-schema block here — it is identical on
     * every round and pure noise next to one round's increment (the drawer
     * detail keeps it: there it is part of the request payload). Small
     * increments auto-expand so the panel answers at a glance; large
     * payloads stay collapsed and render on click. */
    let total = 0;
    for (const m of shown)
      total += (typeof _debugMsgChars === 'function') ? _debugMsgChars(m) : 0;
    if (shown.length <= 6 && total <= 300 * 1024 &&
        typeof _debugOpenBlock === 'function') {
      body.querySelectorAll('.debug-msg-block').forEach(
        (b) => _debugOpenBlock(b));
    }
  }
}

/* ── Turn Trace (耗时分析) — the flame-graph view of ONE task ───────────
 * docs/TURN_TRACE_CONTRACT.md. The drawer renders ONLY what
 * /api/v1/tasks/<id>/trace folds SERVER-side from the persisted event
 * log: the client is a reducer over the returned span tree and never
 * derives timing from its own clocks (the charter invariant).
 * Layout: one row per span depth (turn / rounds & waits / llm & tools /
 * sub-segments) + a final gray row for the explicitly-unattributed gaps.
 */
function _trFmtMs(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '…';
  ms = Math.max(0, Math.round(Number(ms)));
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + 's';
  const m = Math.floor(s / 60);
  return m + 'm' + String(Math.round(s % 60)).padStart(2, '0') + 's';
}

const _TR_KIND_I18N = {
  turn: 'ri.trKindTurn', round: 'ri.trKindRound', llm: 'ri.trKindLlm',
  llm_ttft: 'ri.trKindLlmTtft', tool: 'ri.trKindTool',
  retry_wait: 'ri.trKindRetryWait', compaction: 'ri.trKindCompaction',
  approval_wait: 'ri.trKindApprovalWait', spawn_wait: 'ri.trKindSpawnWait',
};

function _trKindLabel(kind) {
  const key = _TR_KIND_I18N[kind];
  const v = key ? t(key) : '';
  return (v && v !== key) ? v : String(kind || '');
}

function _trBarTitle(sp, elapsed) {
  const lines = [
    `${sp.name || sp.kind} · ${_trKindLabel(sp.kind)} · ${_trFmtMs(elapsed)}`,
  ];
  const a = sp.attrs || {};
  if (a.query) lines.push(a.query);
  if (a.model) lines.push(a.model);
  if (Array.isArray(a.attempts)) {
    for (const at of a.attempts) {
      lines.push(`${at.tag || at.model || 'attempt'}: ` +
        `${at.tokensIn}→${at.tokensOut} · ${_trFmtMs(at.streamElapsedMs)}`);
    }
  }
  if (a.attempt != null) lines.push(t('ri.trTipAttempt', { n: a.attempt }));
  if (a.verdict) lines.push(a.verdict);
  if (sp.budgetMs != null) {
    lines.push(sp.overBudget
      ? t('ri.trTipOverBudget', {
          budget: _trFmtMs(sp.budgetMs),
          over: _trFmtMs(Math.max(0, elapsed - sp.budgetMs)) })
      : t('ri.trTipBudget', { budget: _trFmtMs(sp.budgetMs) }));
  }
  if (sp.truncated) lines.push(t('ri.trTipTruncated'));
  if (sp.status === 'running') lines.push(t('ri.trTipRunning'));
  return lines.join('\n');
}

async function _riOpenTrace(taskId) {
  _riSel.traceOpen = true;
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.remove('ri-sel'));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.add('ri-sel'));
  }
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.traceTitle');
  if (content) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.loading'))}</div>`;
  }
  _riSetDetailActive(true);
  const doc = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getTrace(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId || !_riSel.traceOpen) return;
  _riRenderTrace(doc);
}

function _riRenderTrace(doc) {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (!content) return;
  if (title) title.textContent = t('ri.traceTitle');
  if (!doc || !doc.eventsAvailable) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.expired'))}</div>`;
    return;
  }
  const t0 = Number(doc.tStart) || 0;
  const total = Math.max(1, Number(doc.totalMs) || 1);
  const s = doc.summary || {};

  /* Summary chips — the disjoint bucket partition (sums to totalMs by
   * contract), each chip colored like its flame kind. */
  const chips = [];
  const pushChip = (cls, label, val, sub) => {
    if (val == null) return;
    chips.push(
      `<span class="tr-chip ${cls}">` +
      `<span class="tr-chip-dot" aria-hidden="true"></span>` +
      `${_riEsc(label)} <b>${_riEsc(_trFmtMs(val))}</b>` +
      (sub ? ` <i>${_riEsc(sub)}</i>` : '') + `</span>`);
  };
  pushChip('tr-c-total', t('ri.traceTotal'), doc.totalMs);
  pushChip('tr-c-llm', t('ri.trKindLlm'), s.llmMs,
    s.ttftMs ? t('ri.traceTtftSub', { v: _trFmtMs(s.ttftMs) }) : '');
  pushChip('tr-c-tool', t('ri.trKindTool'), s.toolMs);
  if (s.waitMs) pushChip('tr-c-wait', t('ri.trKindRetryWait'), s.waitMs);
  if (s.compactionMs) {
    pushChip('tr-c-compact', t('ri.trKindCompaction'), s.compactionMs);
  }
  if (s.approvalWaitMs) {
    pushChip('tr-c-approval', t('ri.trKindApprovalWait'), s.approvalWaitMs);
  }
  if (s.unattributedMs) {
    pushChip('tr-c-gap', t('ri.trKindGap'), s.unattributedMs);
  }

  let html = `<div class="tr-wrap"><div class="tr-chips">${chips.join('')}</div>`;

  /* Honesty notes (the Request Inspector disclosure precedent). */
  if (doc.running) {
    html += `<div class="tr-note">${_riEsc(t('ri.traceLive'))}</div>`;
  }
  if (doc.coverage === 'partial') {
    const key = doc.coverageReason === 'flow'
      ? 'ri.tracePartialFlow' : 'ri.tracePartialLegacy';
    html += `<div class="tr-note tr-note-warn">${_riEsc(t(key))}</div>`;
  }
  const over = Array.isArray(s.overBudget) ? s.overBudget : [];
  if (over.length) {
    const items = over.map((o) =>
      `${_riEsc(o.name)} ${_riEsc(_trFmtMs(o.elapsedMs))}` +
      ` / ${_riEsc(_trFmtMs(o.budgetMs))}`).join(' · ');
    html += `<div class="tr-note tr-note-over">` +
      `${_riEsc(t('ri.traceOverBudget', { n: over.length }))}: ${items}</div>`;
  }

  /* Flame rows: depth 0..N + the gap row. */
  const spans = Array.isArray(doc.spans) ? doc.spans : [];
  const gaps = Array.isArray(doc.gaps) ? doc.gaps : [];
  let maxDepth = 0;
  for (const sp of spans) maxDepth = Math.max(maxDepth, sp.depth || 0);
  maxDepth = Math.min(maxDepth, 3);
  const rowLabelKeys = ['ri.trRowTurn', 'ri.trRowPhase', 'ri.trRowDetail',
    'ri.trRowSub'];
  const barHtml = (sp) => {
    const a = sp.tStart == null ? t0 : sp.tStart;
    const b = sp.tEnd == null ? (t0 + total) : sp.tEnd;
    const elapsed = Math.max(0, b - a);
    const left = Math.min(100, Math.max(0, (a - t0) / total * 100));
    const width = Math.min(100 - left,
      Math.max(0.15, (b - a) / total * 100));
    const cls = 'tr-bar tr-k-' + (sp.kind || 'unknown') +
      (sp.status === 'error' || sp.status === 'aborted' ? ' tr-err' : '') +
      (sp.overBudget ? ' tr-over' : '') +
      (sp.status === 'running' ? ' tr-live' : '') +
      (sp.truncated ? ' tr-trunc' : '');
    const inner = width > 7
      ? `<span class="tr-bar-txt">${_riEsc(sp.name || '')} ` +
        `${_riEsc(_trFmtMs(elapsed))}</span>` : '';
    return `<div class="${cls}" style="left:${left.toFixed(3)}%;` +
      `width:${width.toFixed(3)}%" title="${_riEsc(_trBarTitle(sp, elapsed))}">` +
      `${inner}</div>`;
  };
  html += '<div class="tr-flame">';
  for (let d = 0; d <= maxDepth; d++) {
    html += `<div class="tr-row"><span class="tr-row-label">` +
      `${_riEsc(t(rowLabelKeys[d]))}</span><div class="tr-track">`;
    for (const sp of spans) {
      if ((sp.depth || 0) === d) html += barHtml(sp);
    }
    html += '</div></div>';
  }
  /* The unattributed row — ALWAYS rendered when gaps exist, so a hole in
   * the accounting is visible, never silent (contract invariant #2). */
  if (gaps.length) {
    html += `<div class="tr-row"><span class="tr-row-label">` +
      `${_riEsc(t('ri.trKindGap'))}</span><div class="tr-track">`;
    for (const g of gaps) {
      const left = Math.min(100, Math.max(0, (g.tStart - t0) / total * 100));
      const width = Math.min(100 - left,
        Math.max(0.15, (g.tEnd - g.tStart) / total * 100));
      html += `<div class="tr-bar tr-k-gap" style="left:${left.toFixed(3)}%;` +
        `width:${width.toFixed(3)}%" title="` +
        `${_riEsc(t('ri.trKindGap') + ' · ' + _trFmtMs(g.tEnd - g.tStart))}">` +
        `</div>`;
    }
    html += '</div></div>';
  }
  /* Axis: 0 / mid / total. */
  html += `<div class="tr-axis"><span>0</span>` +
    `<span>${_riEsc(_trFmtMs(total / 2))}</span>` +
    `<span>${_riEsc(_trFmtMs(total))}</span></div>`;
  html += '</div></div>';
  content.innerHTML = html;
}
