/* ═══════════════════════════════════════════════════════════════════
   core/request_inspector.js — Request Inspector drawer (P2)

   docs/DEBUG_PANEL_REDESIGN.md (owner-ratified form A): a Network-style
   per-task request list in a RIGHT-SIDE drawer (squeezes chatinner, never
   covers it). Two-level task axis: Task rows → Request rows → detail.

   SERVER-AUTHORITATIVE: task list and round rows fold from
   /api/v1/tasks/* (the persisted task_events log, 6h). The in-memory
   _debugRequests (debug_panel.js, P1) is ONLY a live accelerator —
   sse_poll_fallback never processes messages_snapshot, so the client log
   has gaps the server log never does.

   The detail pane REUSES showMessagesInDebug (debug_panel.js) — there is
   NO second message/JSON renderer. The legacy .debug-panel markup lives
   inside this drawer's right pane (index.html); CSS re-poses it static.

   This file is concatenated by lib/js_bundler.py AFTER core/debug_panel.js
   — symbols share window scope, no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

let _riOpen = false;
const _riSel = { taskId: null, fold: null };

function toggleRequestInspector() {
  if (_riOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riOpen = true;
  /* Keep the legacy debugVisible flag in sync — other readers (restore
   * paths, the _applyDebugModeVisibility helper) key off it. */
  if (typeof debugVisible !== 'undefined') debugVisible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riLoadTasks(typeof activeConvId !== 'undefined' ? activeConvId : null);
}

function closeRequestInspector() {
  _riOpen = false;
  if (typeof debugVisible !== 'undefined') debugVisible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riOpen) return;
  _riSel.taskId = null;
  _riSel.fold = null;
  _riLoadTasks(convId);
}

/* Turn badge label for a request row (P4): endpoint phases read through
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

/* ── Level 1: task rows for the active conversation ── */
async function _riLoadTasks(convId) {
  const list = _riEl('riTaskList');
  const rounds = _riEl('riRoundList');
  if (!list) return;
  if (rounds) rounds.innerHTML = '';
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
  list.innerHTML = '';
  for (const row of tasks) {
    const el = document.createElement('div');
    el.className = 'ri-task' + (_riSel.taskId === row.taskId ? ' ri-sel' : '') +
      (row.isSwarmAgent ? ' ri-task-agent' : '');
    const liveBadge = row.live
      ? `<span class="ri-live-badge">${_riEsc(t('ri.live'))}</span>` : '';
    const reqN = row.requestCount || 0;
    const approx = (row.legacyCount || 0) > 0;
    const countLabel = approx
      ? `≈${reqN + row.legacyCount}`
      : `${reqN}`;
    const expired = !row.hasEvents && !row.live;
    const agentBadge = row.isSwarmAgent
      ? `<span class="ri-agent-badge">${_riEsc(row.agentId || 'agent')}</span> · ` : '';
    el.innerHTML =
      `<div class="ri-task-top">` +
      `<span class="ri-task-id">${_riEsc(String(row.taskId).slice(0, 8))}</span>` +
      liveBadge +
      `<span class="ri-task-time">${_riEsc(_riTimeLabel(row.createdAt))}</span>` +
      `</div>` +
      `<div class="ri-task-sub">` +
      agentBadge +
      (expired
        ? `<span class="ri-expired">${_riEsc(t('ri.expired'))}</span>`
        : `<span title="${_riEsc(t('ri.requests'))}">${countLabel} ${_riEsc(t('ri.requests'))}</span>`) +
      ` · ${_riEsc(row.status || '')}` +
      `</div>`;
    el.onclick = () => _riSelectTask(row.taskId);
    list.appendChild(el);
  }
}

/* ── Level 2: request rows (metadata) for the selected task ── */
async function _riSelectTask(taskId) {
  _riSel.taskId = taskId;
  _riSel.fold = null;
  /* Re-mark the selected task row. */
  const list = _riEl('riTaskList');
  if (list) {
    list.querySelectorAll('.ri-task').forEach((el) => {
      el.classList.toggle('ri-sel',
        el.querySelector('.ri-task-id') &&
        taskId.startsWith(el.querySelector('.ri-task-id').textContent));
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
    rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.expired'))}</div>`;
    return;
  }
  /* Coverage chip — honest disclosure (design §7). 'endpoint-untagged':
   * a pre-P4 endpoint log whose planner/worker/critic rounds exist but
   * share numbers with no phase tag (ambiguous, NOT uncovered). */
  if (fold.coverage === 'partial') {
    const reasonKey = fold.coverageReason === 'endpoint-untagged'
      ? 'ri.coverageAmbiguous' : 'ri.coveragePartial';
    const chip = document.createElement('div');
    chip.className = 'ri-coverage-chip';
    chip.innerHTML =
      (typeof Icon === 'function' ? Icon('alertTriangle', 12) : '') +
      ` <span>${_riEsc(t(reasonKey))}</span>`;
    rounds.appendChild(chip);
  }
  const reqs = Array.isArray(fold.requests) ? fold.requests : [];
  if (!reqs.length) {
    const emp = document.createElement('div');
    emp.className = 'ri-empty';
    emp.textContent = t('ri.empty');
    rounds.appendChild(emp);
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
    const turnBadge = row.turn
      ? `<span class="ri-turn-badge">${_riEsc(_riTurnLabel(row))}</span>` : '';
    el.innerHTML =
      `<div class="ri-round-top">` +
      turnBadge +
      `<span class="ri-round-n">R${_riEsc(row.roundNum)}</span>` +
      `<span class="ri-round-model">${_riEsc(row.model || '?')}</span>` +
      `<span class="ri-round-meta">${row.messageCount} msgs · ~${tok}tok` +
      (row.toolsCount ? ` · ${row.toolsCount} tools` : '') + `</span>` +
      `</div>` +
      (attemptBits ? `<div class="ri-round-attempts">${attemptBits}</div>` : '');
    el.onclick = () => _riSelectRound(taskId, row.roundNum, el, row.turn || '');
    rounds.appendChild(el);
  }
  /* State mirrors (NOT requests) — collapsed at the bottom, clearly labeled. */
  const states = Array.isArray(fold.states) ? fold.states : [];
  if (states.length) {
    const head = document.createElement('div');
    head.className = 'ri-states-head';
    head.textContent = `${t('ri.states')} (${states.length}) — ${t('ri.stateNote')}`;
    rounds.appendChild(head);
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
      rounds.appendChild(el);
    }
  }
}

/* ── Level 3: detail — REUSES showMessagesInDebug (no second renderer) ── */

/* Payload cache (P3): payloads are re-fetched on demand from the server,
 * so repeated round clicks + the diff's N-1 lookup stay cheap. Live
 * accelerator entries (SSE-fed _debugRequests) win over the network.
 * FIFO-capped; entries are never mutated after insert. */
const _riPayloadCache = {};
const _RI_PAYLOAD_CACHE_MAX = 40;
async function _riFetchPayload(taskId, roundNum, turn, kind) {
  turn = turn || '';
  kind = kind || 'request';
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Live accelerator FIRST — an SSE-fed entry is fresher than anything we
   * previously cached from the network (a new round may have re-emitted
   * the payload since the cache entry was stored). Turn-tagged rounds key
   * as 'turn|roundNum' in the P1 log (endpoint phases re-number from 1);
   * state mirrors land in .states (same roundNum axis as the producing
   * request — design §3.1), the latest mirror for a round wins. */
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

/* Longest shared leading prefix between two message arrays, compared by
 * canonical JSON. Round N's payload is round N-1's payload + the new
 * assistant/tool/user messages appended by that round, so a positional
 * prefix compare is exact in the normal case; any divergence degrades
 * safely to K=0 (no fold). */
function _riSharedPrefix(prevMsgs, curMsgs) {
  const n = Math.min(prevMsgs.length, curMsgs.length);
  let k = 0;
  while (k < n && JSON.stringify(prevMsgs[k]) === JSON.stringify(curMsgs[k])) k++;
  return k;
}

/* Scope one round's payload to what THAT round appended. Round N's
 * messages are round N-1's + the messages that round produced/consumed, so
 * the shared leading prefix is conversation history the user did NOT click
 * for. The jump-button panel shows only the increment (owner, 2026-07-28:
 * "records only for this round of tool calls are sufficient"). Round 1,
 * a missing/expired previous payload, or a zero shared prefix all return
 * null — the caller then falls back to the state mirror's TAIL slice, never
 * the full system-prompt dump (owner, 2026-08-04). */
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

/* Degraded fallback for the state axis when no exact increment exists
 * (round 1, a missing previous payload, a diverged prefix): the post-tool
 * mirror's TAIL is still exactly this round — the assistant message carrying
 * the tool_calls, plus the tool results appended after it. Anything older
 * (system prompt, conversation history) is noise in a panel that exists to
 * answer ONE round (owner, 2026-08-04: "just the tool call and the result,
 * no system prompt"). */
function _riTailSlice(messages) {
  if (!Array.isArray(messages)) return [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'assistant' &&
        Array.isArray(m.tool_calls) && m.tool_calls.length)
      return messages.slice(i);
  }
  /* No tool call in the mirror (final-answer round): the increment boundary
   * is the last user message — still never the system prompt or older
   * history. */
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i] && messages[i].role === 'user') {
      const tail = messages.slice(i + 1);
      if (tail.length) return tail;
      break;
    }
  }
  /* The mirror holds nothing past its last user message (e.g. a lone-user
   * mirror): show the non-system messages — an EMPTY panel is never the
   * right answer, and the system prompt is still never shown. */
  return messages.filter((m) => m && m.role !== 'system');
}

async function _riSelectRound(taskId, roundNum, el, turn) {
  turn = turn || '';
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.toggle('ri-sel', r === el));
  }
  const payload = await _riFetchPayload(taskId, roundNum, turn);
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale
  if (!payload || !payload.messages) return;
  /* P3 prefix-fold: diff this round's payload against the SAME phase's
   * round N-1 (endpoint phases each re-number from 1), so the shared
   * prefix collapses and the increment highlights. resetScroll: switching
   * rounds is a context switch — snap the detail pane to the top instead
   * of keeping the previous round's (meaningless here) scroll offset. */
  let opts = { resetScroll: true };
  const num = parseInt(roundNum, 10);
  if (Number.isFinite(num) && num > 1) {
    const prev = await _riFetchPayload(taskId, num - 1, turn);
    if (!_riOpen || _riSel.taskId !== taskId) return;  // stale
    if (prev && prev.messages) {
      const k = _riSharedPrefix(prev.messages, payload.messages);
      if (k > 0) { opts.foldPrefix = k; opts.diffBase = 'R' + (num - 1); }
    }
  }
  if (typeof showMessagesInDebug === 'function')
    showMessagesInDebug(payload.messages, payload.label || '', false,
      typeof activeConvId !== 'undefined' ? activeConvId : null,
      payload.tools || undefined, false, undefined, opts);
}

/* ── Tool-row anchor (P6): the owner's core ask ──────────────────────────
 * "I see a suspicious tool call in chatinner — which request produced it?"
 * Every tool row carries `llmRound`; request snapshots carry a 1-based
 * `roundNum`; so row.llmRound + 1 IS the producing request. These two
 * helpers are what tool_rounds.js::_renderToolRequestAnchor calls.
 *
 * _riTaskIdForRound: a tool round does not carry _taskId itself — it lives on
 * the OWNING assistant message. Resolve by scanning the active conversation
 * tail-up for the message whose toolRounds contains this round object (identity
 * first — ANY role, because a non-assistant owner such as the autopilot VU
 * bubble means "owned but not inspectable": its sub-task persists no snapshots,
 * so the round must NOT fall through to the numeric match — then roundNum),
 * because tail-up finds the live turn first. Returns '' when unresolvable, and
 * the caller then renders NO anchor (an anchor that
 * cannot resolve is worse than none). */
function _riTaskIdForRound(round) {
  try {
    const conv = (typeof conversations !== 'undefined') &&
      conversations.find((c) => c && c.id ===
        (typeof activeConvId !== 'undefined' ? activeConvId : null));
    const msgs = (conv && Array.isArray(conv.messages)) ? conv.messages : [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (!m || !Array.isArray(m.toolRounds)) continue;
      if (m.toolRounds.indexOf(round) !== -1) {
        /* Identity owner found. A NON-assistant owner (the autopilot VU
         * bubble — role 'user', _isVirtualUser) owns the round but its
         * sub-task persists NO request/state snapshots: owned yet not
         * inspectable → '' (the contract: no anchor beats a wrong one).
         * Falling through to the numeric match here pinned VU rounds on the
         * WORKER task's same-numbered mirror — the 2026-08-03 incident,
         * where a VU verification round opened the worker's state and read
         * as data corruption. */
        return (m.role === 'assistant' && !m._isVirtualUser)
          ? (m._taskId || '') : '';
      }
      if (m.role !== 'assistant') continue;
      if (round && round.roundNum != null &&
          m.toolRounds.some((r) => r && r.roundNum === round.roundNum &&
                                   r.llmRound === round.llmRound)) {
        return m._taskId || '';
      }
    }
  } catch (e) {
    console.warn('[ri] taskId-for-round resolve failed:', e);
  }
  return '';
}

/* Open the inspector directly at the request that PRODUCED a tool call.
 * Same positioning/flash treatment as the bubble anchor, but addressed by
 * (taskId, roundNum) instead of a message id — so it works for any row in
 * any turn, including endpoint phases and VU sub-tasks. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  if (!taskId || roundNum == null) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(taskId);
  const fold = _riSel.fold;
  const reqs = (fold && Array.isArray(fold.requests)) ? fold.requests : [];
  /* Prefer an exact roundNum match; endpoint tasks re-number per phase, so
   * a worker-phase row wins over planner/critic when several share a number. */
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
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        typeof activeConvId !== 'undefined' ? activeConvId : null,
        view.payload.tools || undefined, false, undefined, { resetScroll: true });
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

/* ── Bubble anchor (P3): jump from an assistant bubble to the exact
 * request(s) that produced it. msgId → msg._taskId → task fold → round
 * (the bubble's last apiRound.round, 1-based == snapshot roundNum; falls
 * back to the task's last request round). Works for ANY task id —
 * including VU sub-tasks that never appear in the by-conv task list. */
async function openRequestInspectorForMessage(msgId) {
  const conv = (typeof conversations !== 'undefined') &&
    conversations.find((c) => c && c.id ===
      (typeof activeConvId !== 'undefined' ? activeConvId : null));
  const msg = conv && Array.isArray(conv.messages) &&
    conv.messages.find((m) => m && m._msgId === msgId);
  if (!_riOpen) openRequestInspector();
  if (!msg || !msg._taskId) return;  // conv-level view is already loading
  const taskId = msg._taskId;
  const apiRounds = Array.isArray(msg.apiRounds) ? msg.apiRounds : [];
  const lastApiRound = apiRounds.length
    ? apiRounds[apiRounds.length - 1].round : null;
  /* Turn hint (P4): endpoint bubbles carry phase markers — planner turns
   * (_isEndpointPlanner), critic/review turns (_isEndpointReview); a plain
   * assistant bubble in an endpoint task is a WORKER turn. */
  const turnHint = msg._isEndpointPlanner ? 'planning'
    : (msg._isEndpointReview ? 'reviewing' : '');
  await _riSelectTask(taskId);
  const fold = _riSel.fold;
  const reqs = (fold && Array.isArray(fold.requests)) ? fold.requests : [];
  let target = null, targetTurn = '';
  if (lastApiRound != null) {
    const pick = reqs.find((r) => String(r.roundNum) === String(lastApiRound) &&
      (turnHint ? r.turn === turnHint : true)) ||
      reqs.find((r) => String(r.roundNum) === String(lastApiRound));
    if (pick) { target = pick.roundNum; targetTurn = pick.turn || ''; }
  }
  if (target == null && reqs.length) {
    const w = [...reqs].reverse().find((r) => r.turn === 'working') ||
      reqs[reqs.length - 1];
    target = w.roundNum;
    targetTurn = w.turn || '';
  }
  if (target == null) return;
  const el = document.querySelector(
    '#riRoundList .ri-round[data-round="' + String(target) +
    '"][data-turn="' + targetTurn + '"]') ||
    document.querySelector(
      '#riRoundList .ri-round[data-round="' + String(target) + '"]');
  if (el) {
    /* scrollIntoView is absent in some environments (jsdom) — guard. */
    if (typeof el.scrollIntoView === 'function')
      el.scrollIntoView({ block: 'nearest' });
    el.classList.add('ri-flash');
    setTimeout(() => el.classList.remove('ri-flash'), 1600);
  }
  await _riSelectRound(taskId, target, el, targetTurn);
}
