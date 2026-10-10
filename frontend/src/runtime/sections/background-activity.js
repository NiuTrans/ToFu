/* ===== migrated source: background-activity.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   background-activity.js — "still working" chip in the convStatusStrip

   Answers the question a settled foreground turn leaves behind: are any
   sub-agents (spawn_agents swarms) or detached background commands still
   executing right now? The spawning turn has usually ENDED by the time
   this matters — swarm results arrive later as auto-dispatched inbox
   injects. This chip filters by the active conversation's conv_id while it
   polls the owner-scoped snapshot `GET /api/v4/activity/background`
   and lives in the conversation-status strip directly above the composer
   (same in-flow host as the context gauge, inserted before #turnNav).

   Contract: docs/contracts/API_CONTRACT.md §2.19 + routes/api_v1/activity.py. Both
   lists are empty in the steady state — the chip is display:none then
   and the poll backs off to a slow idle cadence.

   Anti-flicker invariants (same discipline as context-bar.js):
   1. The chip node is CREATED ONCE in `_ensureChip()`; steady-state
      updates only set textContent / title / visibility.
   2. The popover list is rebuilt ONLY when its data signature changes —
      a live poll must never collapse a row the user is reading.
   3. ONE chained-setTimeout poll loop (never setInterval): ~3s while
      anything is active, ~10s idle, ticks skipped while the tab is
      hidden (no pending timeout), and an immediate tick on visibility return.

   Public API:
     runtimeScope.updateBackgroundActivity() — force one snapshot tick.
   ═══════════════════════════════════════════════════════════════════ */
// One snapshot-derived index shared with the retained sidebar adapter. Deleted
// with this adapter when background chrome is composed as a typed surface.
let backgroundConversationActivity = new Map();
(function () {
  'use strict';

  /* _state: cached chip nodes (see context-bar.js for the pattern). */
  let _state = null;

  let _disposed = false;
  /* Last applied snapshot + its repaint signature. */
  let _last = { swarms: [], backgroundCommands: [] };
  let _lastSig = '';
  let _popover = null;   // { el, bodyNode, sig }

  function _tt(key, fallback, vars) {
    if (typeof t === 'function') return t(key, vars);
    if (!vars) return fallback;
    return String(fallback).replace(/\{(\w+)\}/g,
      (m, name) => (name in vars ? String(vars[name]) : m));
  }

  function _ensureChip() {
    if (_state && _state.el && _state.el.isConnected) return _state;
    const host = document.getElementById('convStatusStrip');
    if (!host) return null;
    const existing = document.getElementById('bgActivityChip');
    const el = existing || document.createElement('aside');
    if (!existing) {
      el.id = 'bgActivityChip';
      el.setAttribute('role', 'button');
      el.tabIndex = 0;
      el.setAttribute('aria-expanded', 'false');
      el.setAttribute('aria-controls', 'bgActivityPopover');
      el.addEventListener('keydown', _onChipKeydown);
      el.className = 'bg-activity-chip';
      el.setAttribute('aria-live', 'polite');
      /* Left-to-right strip order: context gauge (prepended), this chip,
       * then the turn-nav right cell. */
      host.insertBefore(el, document.getElementById('turnNav') || null);
    }
    if (!el.querySelector('.bg-act-dot')) {
      const dot = document.createElement('span');
      dot.className = 'bg-act-dot';
      dot.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span');
      label.className = 'bg-act-label';
      el.appendChild(dot);
      el.appendChild(label);
    }
    el.style.display = 'none';
    _state = {
      el,
      labelNode: el.querySelector('.bg-act-label'),
      lastLabel: '',
      lastTitle: '',
      visible: null,
    };
    el.addEventListener('click', _onChipClick);
    return _state;
  }

  /* ── Snapshot → chip ──────────────────────────────────────────── */

  function _summarize(data) {
    const convId = typeof activeConvId !== 'undefined' ? activeConvId : null;
    return summarizeBackgroundActivity(data, convId);
  }

  function _chipLabel(sum) {
    return backgroundActivityLabel(sum, _tt);
  }

  function _chipTitle(sum) {
    const lines = [_tt('bgActivity.title', 'Background activity')];
    for (const s of sum.swarms) {
      lines.push('· ' + backgroundActivitySummary(s, _tt));
    }
    for (const c of sum.commands) {
      lines.push('· $ ' + (c.command || ''));
    }
    return lines.join('\n');
  }

  function _apply(sum) {
    const s = _ensureChip();
    if (!s) return;
    const active = sum.agentsActive > 0 || sum.commandsActive > 0;
    if (s.visible !== active) {
      s.el.style.display = active ? '' : 'none';
      s.visible = active;
    }
    if (!active) {
      _closePopover();
      return;
    }
    const label = _chipLabel(sum);
    if (s.lastLabel !== label) {
      s.labelNode.textContent = label;
      s.lastLabel = label;
    }
    const tip = _chipTitle(sum);
    if (s.lastTitle !== tip) {
      s.el.title = tip;
      s.lastTitle = tip;
    }
    if (_popover) _renderPopoverBody(sum);
  }

  /* ── Popover ──────────────────────────────────────────────────── */

  function _fmtElapsed(totalSeconds) {
    const sec = Math.max(0, Math.round(Number(totalSeconds) || 0));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    const r = sec % 60;
    if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
    return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function _swarmElapsed(swarm) {
    const created = Number(swarm.created_at) || 0;
    if (!created) return '';
    return _fmtElapsed(Date.now() / 1000 - created);
  }

  function _convLabel(convId) {
    if (!convId) return '';
    if (typeof getConvById === 'function') {
      const conv = getConvById(convId);
      const title = conv && (conv.title || conv.summary);
      if (title) return title;
    }
    return convId;
  }

  function _sigOf(sum) {
    /* Only fields that affect the popover body — chip text follows the
     * same signature cheaply via label/title guards. */
    return JSON.stringify([
      sum.swarms.map(s => [s.swarm_key, s.conv_id, _convLabel(s.conv_id),
        s.created_at, s.running, s.pending, s.completed,
        (s.agents || []).map(a => [a.id, a.role, a.objective, a.status, a.phase, a.round, a.waitReason, a.dependsOn])]),
      sum.commands.map(c => [c.command_id, c.command, c.status]),
    ]);
  }

  function _closePopover() {
    _state?.el.setAttribute('aria-expanded', 'false');
    if (_popover && _popover.el) {
      try { _popover.el.remove(); } catch (e) { /* detached */ }
    }
    _popover = null;
    document.removeEventListener('click', _onDocClick, true);
  }

  function _onDocClick(e) {
    if (_popover && _popover.el && !_popover.el.contains(e.target)
        && _state && _state.el && !_state.el.contains(e.target)) {
      _closePopover();
    }
  }

  function _onChipKeydown(e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _togglePopover(); }
    else if (e.key === 'Escape') _closePopover();
  }

  function _onChipClick(e) {
    e.stopPropagation();
    _togglePopover();
  }

  function _togglePopover() {
    if (_popover) { _closePopover(); return; }
    const s = _ensureChip();
    if (!s || !s.visible) return;
    const el = document.createElement('div');
    el.id = 'bgActivityPopover';
    el.className = 'bg-act-popover';
    const head = document.createElement('div');
    head.className = 'bg-act-pop-head';
    head.textContent = _tt('bgActivity.title', 'Background activity');
    const body = document.createElement('div');
    body.className = 'bg-act-pop-body';
    el.appendChild(head);
    el.appendChild(body);
    document.body.appendChild(el);
    const r = s.el.getBoundingClientRect();
    el.style.position = 'fixed';
    el.style.left = Math.round(r.left) + 'px';
    /* Opens UPWARD — the strip sits directly above the composer. */
    el.style.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
    _popover = { el, bodyNode: body, sig: '', swarmClocks: [], commandClocks: [] };

    s.el.setAttribute('aria-expanded', 'true');
    _renderPopoverBody(_summarize(_last), true);
    document.addEventListener('click', _onDocClick, true);
  }

  function _renderPopoverBody(sum, force) {
    if (!_popover) return;
    const sig = _sigOf(sum);
    if (!force && sig === _popover.sig) {
      // Update only clock leaves: preserve open rows, selection and scroll.
      _popover.swarmClocks.forEach(({ node, index }) => {
        const text = _swarmElapsed(sum.swarms[index]);
        if (node.textContent !== text) node.textContent = text;
      });
      _popover.commandClocks.forEach((node, index) => {
        const text = _fmtElapsed(sum.commands[index].elapsed_s);
        if (node.textContent !== text) node.textContent = text;
      });
      return;
    }
    _popover.swarmClocks = [];
    _popover.commandClocks = [];
    _popover.sig = sig;
    const body = _popover.bodyNode;
    body.textContent = '';

    if (sum.swarms.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionAgents', 'Sub-agents');
      body.appendChild(secTitle);
      sum.swarms.forEach((swarm, index) => {
        const row = _swarmRow(swarm);
        const node = row.querySelector('.bg-act-elapsed');
        if (node) _popover.swarmClocks.push({ node, index });
        body.appendChild(row);
      });
    }
    if (sum.commands.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionCommands', 'Background commands');
      body.appendChild(secTitle);
      for (const cmd of sum.commands) {
        const row = _commandRow(cmd);
        _popover.commandClocks.push(row.querySelector('.bg-act-elapsed'));
        body.appendChild(row);
      }
    }
  }

  function _swarmRow(swarm) {
    const row = document.createElement('div');
    row.className = 'bg-act-swarm';
    const head = document.createElement('div');
    head.className = 'bg-act-swarm-head';
    const label = document.createElement('span');
    label.className = 'bg-act-swarm-conv';
    label.textContent = _convLabel(swarm.conv_id) || swarm.swarm_key || '';
    head.appendChild(label);
    const summary = document.createElement('span');
    summary.className = 'bg-act-swarm-summary';
    summary.textContent = backgroundActivitySummary(swarm, _tt);
    head.appendChild(summary);
    const elapsed = _swarmElapsed(swarm);
    if (elapsed) {
      const el = document.createElement('span');
      el.className = 'bg-act-elapsed';
      el.textContent = elapsed;
      head.appendChild(el);
    }
    row.appendChild(head);
    for (const agent of (swarm.agents || [])) {
      row.appendChild(_agentRow(agent));
    }
    /* Deep-link: the swarm outlives its spawning turn, but its
     * conversation is where the auto-dispatched results will land. */
    if (swarm.conv_id && typeof loadConversation === 'function') {
      row.classList.add('is-clickable');
      row.addEventListener('click', (e) => {
        e.stopPropagation();
        _closePopover();
        loadConversation(swarm.conv_id);
      });
    }
    return row;
  }

  function _agentRow(agent) {
    return renderBackgroundActivityAgent(document, agent, _tt);
  }

  function _commandRow(cmd) {
    const row = document.createElement('div');
    row.className = 'bg-act-cmd';
    const text = document.createElement('code');
    text.className = 'bg-act-cmd-text';
    text.textContent = cmd.command || '';
    row.appendChild(text);
    const elapsed = document.createElement('span');
    elapsed.className = 'bg-act-elapsed';
    elapsed.textContent = _fmtElapsed(cmd.elapsed_s);
    row.appendChild(elapsed);
    return row;
  }

  /* ── Poll loop ────────────────────────────────────────────────── */

  const controller = createBackgroundActivityController({
    hidden: () => document.hidden || runtimeScope.nativeVisibility?.isHidden() === true,
    read: () => Promise.resolve().then(() =>
      typeof Api !== 'undefined' && Api?.activity ? Api.activity.background() : null),
    apply(data) {
        _last = data;
        const next = backgroundActivityByConversation(data);
        const changedIds = changedBackgroundActivityConversations(backgroundConversationActivity, next);
        backgroundConversationActivity = next;
        if (changedIds.length && typeof refreshBackgroundConversationStatuses === 'function') {
          refreshBackgroundConversationStatuses(changedIds);
        }
        const sum = _summarize(data);
        const sig = _sigOf(sum);
        const active = sum.agentsActive > 0 || sum.commandsActive > 0;
        if (sig !== _lastSig) {
          _lastSig = sig;
          _apply(sum);
        } else if (active) {
          /* Elapsed strings age even when the set is unchanged. */
          _apply(sum);
        }
        const ownerSummary = summarizeBackgroundActivity(data);
        return !!(ownerSummary.agentsActive || ownerSummary.commandsActive);
    },
  });
  runtimeScope.refreshBackgroundActivity = () => controller.refresh();


  function _activityHint(frame) {
    // Token/progress and generic Turn invalidations are deliberately excluded.
    if (['conv_changed', 'conv_deleted', 'swarm_agent_complete',
      'swarm_agent_error', 'swarm_started', 'swarm_complete'].includes(frame?.type)) {
      controller.refresh();
    }
  }
  if (typeof pushSubscribe === 'function') {
    pushSubscribe('notify', '*', _activityHint);
    pushSubscribe('swarm', '*', _activityHint);
  }
  const _unsubscribeReconnect = typeof pushOnReconnect === 'function'
    ? pushOnReconnect(() => controller.refresh(true)) : null;
  const _unsubscribeNativeVisibility = runtimeScope.nativeVisibility?.subscribe?.(
    () => controller.visibilityChanged());

  function updateBackgroundActivity() {

    if (_disposed) return;
    _closePopover();
    const sum = _summarize(_last);
    _lastSig = _sigOf(sum);
    _apply(sum);
    controller.refresh(true);
  }

  runtimeScope.updateBackgroundActivity = updateBackgroundActivity;

  function _onVisibilityChange() {
    controller.visibilityChanged();
  }

  function _onReady() { controller.refresh(true); }

  function _dispose() {
    if (_disposed) return;
    _disposed = true;
    controller.dispose();

    _unsubscribeReconnect?.();
    _unsubscribeNativeVisibility?.();
    if (typeof pushUnsubscribe === 'function') {
      pushUnsubscribe('notify', '*', _activityHint);
      pushUnsubscribe('swarm', '*', _activityHint);
    }
    _closePopover();
    document.removeEventListener('visibilitychange', _onVisibilityChange);
    document.removeEventListener('DOMContentLoaded', _onReady);
    if (_state) {
      _state.el.removeEventListener('click', _onChipClick);

      _state.el.removeEventListener('keydown', _onChipKeydown);
      _state.el.remove();
    }
    _state = null;
    _last = { swarms: [], backgroundCommands: [] };
    backgroundConversationActivity = new Map();
  }

  if (typeof retainedCompositionLifecycle !== 'undefined') {
    retainedCompositionLifecycle.add(_dispose);
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', _onVisibilityChange);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _onReady, { once: true });
    } else {
      _onReady();
    }
  }
})();
