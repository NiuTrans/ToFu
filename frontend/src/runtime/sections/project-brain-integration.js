/* ===== migrated source: project-brain-integration.js ===== */
/* Deterministic Git integration center for Project Brain.
 *
 * This surface makes automation observable. It displays the canonical
 * checkout separately from immutable candidate/stable refs, the running
 * server's boot fingerprint, writer checkpoints, quarantine reasons, and the
 * exact gates in force. All mutations call deterministic backend endpoints;
 * none of these controls invokes an LLM. */
(function () {
  'use strict';

  var _state = {
    path: '',
    data: null,
    timer: null,
    generation: 0,
    wired: false,
    busy: false,
  };

  /* English fallback table. The locale packs (projectBrain.integration.* in
     en.json / zh.json) are authoritative in production; this table keeps the
     surface readable when the i18n chunk has not loaded yet — and in bare
     test harnesses that never install `t`. Strings MUST stay in sync with
     en.json; the zh translations live ONLY in zh.json (single source). */
  var _FALLBACK = {
    title: 'Integration control', subtitle: 'Git-driven, continuous, zero LLM tokens',
    refresh: 'Refresh', promote: 'Promote candidate', reconcileHead: 'Reconcile HEAD',
    confirmReconcileHead: 'Merge committed canonical HEAD history into candidate after integration gates pass?',
    autoOn: 'Auto integration on',
    autoOff: 'Auto integration off', writers: 'Writers', ready: 'Ready queue',
    candidate: 'Candidate', stable: 'Stable', canonicalClean: 'Canonical clean',
    canonicalDirty: 'Canonical dirty', dirtyNote: 'These files are visible, but are not part of candidate or stable.',
    modified: 'modified', deleted: 'deleted', untracked: 'untracked',
    server: 'Running server', servesStable: 'serving stable',
    servesCandidate: 'serving candidate', servesLocal: 'loaded from dirty local source',
    serverUnknown: 'relation to refs is unknown', worktrees: 'Git worktrees',
    prunable: 'prunable', gates: 'Gates', builtInOnly: 'built-in checks only',
    integrationTests: 'integration command configured', stableTests: 'stable command configured',
    prune: 'Prune stale records', confirmPrune: 'Prune Git metadata for worktree directories that are already missing?',
    unregistered: 'Unregistered worktrees', unregisteredHint: 'Visible to Git, but not owned by this integration queue.',
    showingFirst: 'showing the first 20',
    workspaces: 'Writer workspaces', noWorkspaces: 'No writer workspace is registered.',
    task: 'Work ID', taskPlaceholder: 'e.g. pw_a1b2c3',
    titleLabel: 'Title', titlePlaceholder: 'Short description (optional)',
    worktreeLabel: 'Worktree',
    pathPlaceholder: 'Existing worktree path (leave empty to create one)',
    create: 'Create / register writer', managedHint: 'Empty path creates a managed detached worktree from candidate.',
    checkpoint: 'Checkpoint', submit: 'Submit', retry: 'Retry', discard: 'Discard',
    confirmDiscard: 'Discard this workspace from the queue? Git refs and files are preserved.',
    recent: 'Recent integration events', noEvents: 'No integration events yet.',
    loading: 'Reading Git state…', loadFailed: 'Could not load integration state',
    actionFailed: 'Integration action failed', confirmPromote: 'Promote candidate to stable after the configured gates pass?',
    confirmPromoteDiverged: 'Canonical HEAD and candidate diverged. Stable promotion will not update the canonical branch. Promote anyway?',
    noCheckpoint: 'not checkpointed', localChanges: 'local changes',
    notScanned: 'not scanned',
    initializedLater: 'falls back to HEAD until the first mutation',
    stableAhead: 'Candidate and stable diverged. Promotion is disabled.',
    headDiverged: 'Canonical HEAD and candidate diverged. Reconcile their histories before adopting stable into the canonical branch.',
    projectGateMissing: 'Project integration command is not configured; semantic code/config changes will quarantine.',
    stableGateMissing: 'Stable integration command is not configured.',
    quarantine: 'Needs attention', pipelineHelp: 'Only submitted checkpoints enter the queue. Active writers continue at any hour.',
    pipelineAria: 'Integration pipeline',
  };

  var _STATE_FALLBACK = {
    running: 'Running', checkpointed: 'Checkpointed', ready: 'Ready',
    integrating: 'Integrating', merged: 'Merged', quarantined: 'Quarantined',
    failed: 'Failed', discarded: 'Discarded', unknown: 'Unknown',
  };

  /* Resolve one panel string through the app i18n seam (t) with the inline
     English table as fallback. `t` returns the KEY itself when the locale
     chunk lacks it — that case must fall back too, or a missing key would
     paint its raw dotted name on screen. */
  function _pbiText(key) {
    var full = 'projectBrain.integration.' + key;
    try {
      if (typeof t === 'function') {
        var value = t(full);
        if (value && value !== full) return value;
      }
    } catch (_e) { /* fall through to the inline English */ }
    return _FALLBACK[key] || _STATE_FALLBACK[key] || key;
  }

  function _esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _sha(value) { return value ? String(value).slice(0, 12) : '—'; }

  function _stateLabel(value) {
    if (value && _STATE_FALLBACK[value]) return _pbiText('state.' + value);
    return value || _pbiText('state.unknown');
  }

  function _fmtTime(value) {
    if (!value) return '';
    try { return new Date(value).toLocaleString(); } catch (_e) { return value; }
  }

  function _icon(name, size) {
    return typeof Icon === 'function' ? Icon(name, size || 14) : '';
  }

  function _setBadge(data) {
    var badge = document.getElementById('pbTabCountIntegration');
    if (!badge) return;
    var counts = data && data.counts ? data.counts : {};
    var dirty = data && data.repo && data.repo.dirty ? data.repo.dirty.total || 0 : 0;
    var prunable = data && data.repo ? data.repo.prunableWorktrees || 0 : 0;
    var actionable = (counts.quarantined || 0) + (counts.failed || 0) +
      (dirty ? 1 : 0) + (prunable ? 1 : 0);
    badge.textContent = actionable > 99 ? '99+' : String(actionable || '');
    badge.hidden = !actionable;
    badge.title = actionable ? _pbiText('quarantine') : '';
  }

  function _pipeline(data) {
    var counts = data.counts || {};
    var refs = data.refs || {};
    var queued = (counts.ready || 0) + (counts.integrating || 0);
    var stages = [
      [_pbiText('writers'), (data.workspaces || []).length, 'layers'],
      [_pbiText('ready'), queued, 'clock'],
      [_pbiText('candidate'), _sha(refs.candidate), 'gitMerge'],
      [_pbiText('stable'), _sha(refs.stable), 'shield'],
    ];
    var html = '<div class="pbi-pipeline" aria-label="' + _esc(_pbiText('pipelineAria')) + '">';
    for (var i = 0; i < stages.length; i++) {
      if (i) html += '<span class="pbi-pipeline-arrow" aria-hidden="true">→</span>';
      html += '<div class="pbi-stage pbi-stage-' + i + '">' +
        '<span class="pbi-stage-icon">' + _icon(stages[i][2], 15) + '</span>' +
        '<span class="pbi-stage-copy"><span class="pbi-stage-label">' +
        _esc(stages[i][0]) + '</span><strong>' + _esc(stages[i][1]) +
        '</strong></span></div>';
    }
    return html + '</div><div class="pbi-pipeline-help">' + _esc(_pbiText('pipelineHelp')) + '</div>';
  }

  function _canonical(data) {
    var repo = data.repo || {};
    var dirty = repo.dirty || {};
    var clean = !!repo.canonicalClean;
    var parts = clean ? '' : [
      (dirty.modified || 0) + ' ' + _pbiText('modified'),
      (dirty.deleted || 0) + ' ' + _pbiText('deleted'),
      (dirty.untracked || 0) + ' ' + _pbiText('untracked'),
    ].join(' · ');
    return '<div class="pbi-canonical ' + (clean ? 'is-clean' : 'is-dirty') + '">' +
      '<div class="pbi-canonical-mark">' + _icon(clean ? 'checkCircle' : 'alertTriangle', 18) + '</div>' +
      '<div><strong>' + _esc(clean ? _pbiText('canonicalClean') : _pbiText('canonicalDirty')) + '</strong>' +
      (parts ? '<span>' + _esc(parts) + '</span>' : '') +
      (!clean ? '<p>' + _esc(_pbiText('dirtyNote')) + '</p>' : '') + '</div></div>';
  }

  function _serverFact(data) {
    var server = data.server || {};
    var fp = server.codeFingerprint || {};
    var relation = _pbiText('serverUnknown');
    var tone = 'neutral';
    if (server.servesStable) { relation = _pbiText('servesStable'); tone = 'good'; }
    else if (server.servesCandidate) { relation = _pbiText('servesCandidate'); tone = 'warn'; }
    else if (server.sameRepository && (server.sourceTreeDirty || fp.dirty)) {
      relation = _pbiText('servesLocal'); tone = 'warn';
    }
    var detail = fp.digest ? _sha(fp.digest) : (server.bootId ? _sha(server.bootId) : '—');
    return '<div class="pbi-fact pbi-fact-' + tone + '"><span>' + _esc(_pbiText('server')) +
      '</span><strong>' + _esc(relation) + '</strong><code>' + _esc(detail) + '</code></div>';
  }

  function _facts(data) {
    var repo = data.repo || {};
    var gates = data.gates || {};
    var worktrees = String(repo.worktreesTotal || 0);
    if (repo.prunableWorktrees) worktrees += ' · ' + repo.prunableWorktrees + ' ' + _pbiText('prunable');
    var gateText = gates.testCommandConfigured ? _pbiText('integrationTests') : _pbiText('builtInOnly');
    if (gates.stableCommandConfigured) gateText += ' · ' + _pbiText('stableTests');
    return '<div class="pbi-facts">' + _serverFact(data) +
      '<div class="pbi-fact"><span>' + _esc(_pbiText('worktrees')) + '</span><strong>' +
      _esc(worktrees) + '</strong><code>' + _esc((repo.root || '').split('/').pop()) + '</code>' +
      (repo.prunableWorktrees ? '<button type="button" class="pbi-fact-action" data-pbi-action="prune">' +
        _esc(_pbiText('prune')) + '</button>' : '') + '</div>' +
      '<div class="pbi-fact"><span>' + _esc(_pbiText('gates')) + '</span><strong>' +
      _esc(gateText) + '</strong><code>' + _esc((gates.builtIn || []).join(' · ')) +
      '</code></div></div>';
  }

  function _taskActions(item) {
    var id = _esc(item.workId);
    var writable = item.state === 'running' || item.state === 'checkpointed' ||
      item.state === 'quarantined' || item.state === 'failed';
    var html = '';
    if (writable) {
      html += '<button type="button" data-pbi-action="checkpoint" data-task="' + id + '">' +
        _icon('save', 12) + '<span>' + _esc(_pbiText('checkpoint')) + '</span></button>';
      html += '<button type="button" class="pbi-primary" data-pbi-action="submit" data-task="' + id + '">' +
        _icon('gitMerge', 12) + '<span>' + _esc(_pbiText('submit')) + '</span></button>';
    }
    if (item.state === 'quarantined' || item.state === 'failed') {
      html += '<button type="button" data-pbi-action="retry" data-task="' + id + '">' +
        _icon('refreshCw', 12) + '<span>' + _esc(_pbiText('retry')) + '</span></button>';
    }
    if (item.state !== 'integrating' && item.state !== 'merged' &&
        item.state !== 'discarded') {
      html += '<button type="button" data-pbi-action="discard" data-task="' + id + '">' +
        _icon('trash', 12) + '<span>' + _esc(_pbiText('discard')) + '</span></button>';
    }
    return html;
  }

  function _workspaceRows(data) {
    var items = data.workspaces || [];
    if (!items.length) return '<div class="pbi-empty">' + _esc(_pbiText('noWorkspaces')) + '</div>';
    return items.map(function (item) {
      var dirty = item.dirty || {};
      var checkpoint = item.checkpointSha ? _sha(item.checkpointSha) : _pbiText('noCheckpoint');
      var cls = /^[a-z]+$/.test(item.state || '') ? item.state : 'unknown';
      return '<article class="pbi-writer pbi-state-' + cls + '">' +
        '<div class="pbi-writer-state"><span class="pbi-state-dot"></span>' +
        _esc(_stateLabel(item.state)) + '</div>' +
        '<div class="pbi-writer-main"><div class="pbi-writer-title"><strong>' +
        _esc(item.title || item.workId) + '</strong><code>' +
        _esc(item.workId) + '</code></div>' +
        '<div class="pbi-writer-path" title="' + _esc(item.workspacePath) + '">' +
        _esc(item.workspacePath) + '</div><div class="pbi-writer-meta"><span>' + _esc(_pbiText('checkpoint')) + ' ' +
        _esc(checkpoint) + '</span><span>' + (dirty.scanned === false
          ? _esc(_pbiText('notScanned'))
          : ((dirty.total || 0) + ' ' + _esc(_pbiText('localChanges')))) +
        '</span><span>' + _esc(_fmtTime(item.updatedAt)) + '</span></div>' +
        (item.error ? '<pre class="pbi-writer-error">' + _esc(item.error) + '</pre>' : '') +
        '</div><div class="pbi-writer-actions">' + _taskActions(item) + '</div></article>';
    }).join('');
  }

  function _eventRows(data) {
    var events = data.events || [];
    if (!events.length) return '<div class="pbi-empty">' + _esc(_pbiText('noEvents')) + '</div>';
    return events.map(function (event) {
      return '<div class="pbi-event"><span class="pbi-event-dot"></span><div><strong>' +
        _esc(event.message || event.kind) + '</strong>' +
        (event.workId ? '<code>' + _esc(event.workId) + '</code>' : '') +
        (event.detail ? '<p>' + _esc(event.detail) + '</p>' : '') +
        '<time>' + _esc(_fmtTime(event.createdAt)) + '</time></div></div>';
    }).join('');
  }

  function _writerForm() {
    return '<form class="pbi-create" id="pbiCreateForm"><div class="pbi-create-fields">' +
      '<label><span>' + _esc(_pbiText('task')) + '</span><input name="workId" required maxlength="96" placeholder="' +
      _esc(_pbiText('taskPlaceholder')) + '"></label>' +
      '<label><span>' + _esc(_pbiText('titleLabel')) + '</span><input name="title" maxlength="160" placeholder="' +
      _esc(_pbiText('titlePlaceholder')) + '"></label>' +
      '<label class="pbi-create-path"><span>' + _esc(_pbiText('worktreeLabel')) + '</span><input name="workspacePath" placeholder="' +
      _esc(_pbiText('pathPlaceholder')) + '"></label>' +
      '<button type="submit" class="pbi-primary">' + _icon('plus', 13) + '<span>' +
      _esc(_pbiText('create')) + '</span></button></div><p>' + _esc(_pbiText('managedHint')) + '</p></form>';
  }

  function _unregistered(data) {
    var repo = data.repo || {};
    var count = repo.unregisteredWorktreesCount || 0;
    var items = repo.unregisteredWorktrees || [];
    if (!count) return '';
    var rows = items.map(function (item) {
      return '<div class="pbi-unknown-row ' + (item.prunable ? 'is-prunable' : '') + '">' +
        '<span class="pbi-state-dot"></span><code title="' + _esc(item.path) + '">' +
        _esc(item.path) + '</code><span>' + _esc(_sha(item.head)) + '</span>' +
        (item.prunable ? '<strong>' + _esc(_pbiText('prunable')) + '</strong>' : '') + '</div>';
    }).join('');
    return '<details class="pbi-unknown"><summary><span>' + _esc(_pbiText('unregistered')) +
      '</span><strong>' + count + '</strong></summary><p>' + _esc(_pbiText('unregisteredHint')) +
      (count > items.length ? ' ' + _esc(_pbiText('showingFirst')) + '.' : '') +
      '</p><div>' + rows + '</div></details>';
  }

  function renderIntegration(data) {
    var host = document.getElementById('projectBrainIntegrationBody');
    if (!host) return;
    _state.data = data || {};
    _setBadge(data || {});
    var refs = data.refs || {};
    var diverged = (refs.stableAheadCandidate || 0) > 0;
    var canPromote = refs.candidate && refs.stable && refs.candidate !== refs.stable && !diverged;
    var canReconcileHead = (refs.headAheadCandidate || 0) > 0 && !diverged;
    var refNote = [];
    if (!refs.candidateInitialized || !refs.stableInitialized) refNote.push(_pbiText('initializedLater'));
    if (diverged) refNote.push(_pbiText('stableAhead'));
    if (refs.headCandidateDiverged) refNote.push(_pbiText('headDiverged'));
    if (!(data.gates || {}).testCommandConfigured) refNote.push(_pbiText('projectGateMissing'));
    if (!(data.gates || {}).stableCommandConfigured) refNote.push(_pbiText('stableGateMissing'));
    host.innerHTML = '<div class="pbi-shell"><header class="pbi-head"><div><h3>' +
      _esc(_pbiText('title')) + '</h3><p>' + _esc(_pbiText('subtitle')) + '</p></div>' +
      '<div class="pbi-head-actions"><span class="pbi-auto ' + (data.autorun ? 'is-on' : 'is-off') + '">' +
      '<span></span>' + _esc(data.autorun ? _pbiText('autoOn') : _pbiText('autoOff')) + '</span>' +
      '<button type="button" data-pbi-action="refresh" title="' + _esc(_pbiText('refresh')) + '">' +
      _icon('refreshCw', 14) + '</button>' + (canReconcileHead
        ? '<button type="button" class="pbi-reconcile" data-pbi-action="reconcile-head">' +
          _icon('gitMerge', 13) + '<span>' + _esc(_pbiText('reconcileHead')) + '</span></button>'
        : '') + '<button type="button" class="pbi-promote" data-pbi-action="promote"' +
      (canPromote ? '' : ' disabled') + '>' + _icon('shield', 13) + '<span>' +
      _esc(_pbiText('promote')) + '</span></button></div></header>' +
      _pipeline(data) + _canonical(data) + (refNote.length ? '<div class="pbi-note">' +
      _esc(refNote.join(' ')) + '</div>' : '') + _facts(data) + _unregistered(data) +
      '<section class="pbi-section"><div class="pbi-section-head"><h4>' + _esc(_pbiText('workspaces')) +
      '</h4></div>' + _writerForm() + '<div class="pbi-writers">' + _workspaceRows(data) +
      '</div></section><section class="pbi-section"><div class="pbi-section-head"><h4>' +
      _esc(_pbiText('recent')) + '</h4></div><div class="pbi-events">' + _eventRows(data) +
      '</div></section></div>';
    _wire();
  }

  function _showError(error) {
    var host = document.getElementById('projectBrainIntegrationBody');
    if (!host) return;
    var message = (error && (error.message || error.error)) || String(error || _pbiText('actionFailed'));
    var old = host.querySelector('.pbi-error-banner');
    if (old) old.remove();
    var banner = document.createElement('div');
    banner.className = 'pbi-error-banner';
    banner.textContent = message;
    host.insertBefore(banner, host.firstChild);
    if (runtimeScope.ProjectBrain && typeof runtimeScope.ProjectBrain._reportFailure === 'function') {
      runtimeScope.ProjectBrain._reportFailure('projectBrain.integrationFailed', _pbiText('actionFailed'), error, true);
    }
  }

  function _api() {
    return typeof Api !== 'undefined' && Api.project ? Api.project : null;
  }

  function _schedule() {
    if (_state.timer) clearTimeout(_state.timer);
    _state.timer = setTimeout(function () {
      var overlay = document.getElementById('projectBrainOverlay');
      if (!overlay || overlay.hidden || !_state.path) return;
      refreshIntegration(_state.path, { quiet: true });
    }, 30000);
  }

  function refreshIntegration(path, opts) {
    var host = document.getElementById('projectBrainIntegrationBody');
    var api = _api();
    if (!host || !path || !api || typeof api.integrationStatus !== 'function') return Promise.resolve(null);
    _state.path = path;
    var generation = ++_state.generation;
    if (!_state.data && !(opts && opts.quiet)) {
      host.innerHTML = '<div class="pbi-loading">' + _esc(_pbiText('loading')) + '</div>';
    }
    return Promise.resolve(api.integrationStatus(path)).then(function (data) {
      if (generation !== _state.generation || path !== _state.path) return null;
      if (!data || data.ok === false) throw new Error((data && data.error) || _pbiText('loadFailed'));
      renderIntegration(data);
      _schedule();
      return data;
    }).catch(function (error) {
      if (generation !== _state.generation) return null;
      _showError(error);
      _schedule();
      return null;
    });
  }

  function _runAction(action, workId) {
    if (_state.busy) return;
    var api = _api();
    if (!api || !_state.path) return;
    var promise;
    if (action === 'checkpoint') promise = api.integrationCheckpoint(_state.path, workId);
    else if (action === 'submit') promise = api.integrationSubmit(_state.path, workId);
    else if (action === 'retry') promise = api.integrationRetry(_state.path, workId);
    else if (action === 'discard') {
      if (!window.confirm(_pbiText('confirmDiscard'))) return;
      promise = api.integrationDiscard(_state.path, workId);
    }
    else if (action === 'reconcile-head') {
      if (!window.confirm(_pbiText('confirmReconcileHead'))) return;
      promise = api.integrationReconcileHead(_state.path);
    }
    else if (action === 'promote') {
      var headDiverged = !!(_state.data && _state.data.refs &&
        _state.data.refs.headCandidateDiverged);
      if (!window.confirm(_pbiText(headDiverged
        ? 'confirmPromoteDiverged' : 'confirmPromote'))) return;
      promise = api.integrationPromote(_state.path, headDiverged);
    } else if (action === 'prune') {
      if (!window.confirm(_pbiText('confirmPrune'))) return;
      promise = api.integrationPrune(_state.path);
    } else if (action === 'refresh') {
      refreshIntegration(_state.path);
      return;
    } else return;
    _state.busy = true;
    var host = document.getElementById('projectBrainIntegrationBody');
    if (host) host.setAttribute('aria-busy', 'true');
    Promise.resolve(promise).then(function () {
      return refreshIntegration(_state.path);
    }).catch(_showError).finally(function () {
      _state.busy = false;
      if (host) host.removeAttribute('aria-busy');
    });
  }

  function _create(form) {
    if (_state.busy) return;
    var api = _api();
    var workId = (form.elements.workId.value || '').trim();
    var title = (form.elements.title.value || '').trim();
    var workspacePath = (form.elements.workspacePath.value || '').trim();
    if (!api || !workId) return;
    _state.busy = true;
    var promise = workspacePath
      ? api.integrationRegister(_state.path, workId, workspacePath, title)
      : api.integrationCreate(_state.path, workId, title);
    Promise.resolve(promise).then(function () {
      form.reset();
      return refreshIntegration(_state.path);
    }).catch(_showError).finally(function () { _state.busy = false; });
  }

  function _wire() {
    var host = document.getElementById('projectBrainIntegrationBody');
    if (!host || _state.wired) return;
    host.addEventListener('click', function (event) {
      var button = event.target && event.target.closest
        ? event.target.closest('[data-pbi-action]') : null;
      if (!button || button.disabled) return;
      _runAction(button.getAttribute('data-pbi-action'), button.getAttribute('data-task') || '');
    });
    host.addEventListener('submit', function (event) {
      if (!event.target || event.target.id !== 'pbiCreateForm') return;
      event.preventDefault();
      _create(event.target);
    });
    _state.wired = true;
  }

  /* Re-render on a language switch while the panel is open: every label is
     resolved at render time, so without this the surface would stay in the
     previous language until the next poll tick (8s) or manual refresh. */
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('tofu:language-change', function () {
      var overlay = document.getElementById('projectBrainOverlay');
      if (!overlay || overlay.hidden || !_state.data) return;
      try { renderIntegration(_state.data); } catch (_e) { /* best-effort */ }
    });
  }
  runtimeScope.ProjectBrainIntegration = {
    refreshIntegration: refreshIntegration,
    renderIntegration: renderIntegration,
    _state: _state,
  };
})();
