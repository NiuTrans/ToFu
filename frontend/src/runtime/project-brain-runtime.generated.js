// @ts-check
/* Generated lazy retained runtime: project-brain. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { _i18nLang, t } from '../i18n/index';
import { escapeHtml } from '../html-safety';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('project-brain runtime dependency is unavailable: Api');
const Icon = runtimeScope.Icon;
if (typeof Icon !== 'function') throw new Error('project-brain runtime dependency is unavailable: Icon');
const getActiveConv = runtimeScope.getActiveConv;
if (typeof getActiveConv !== 'function') throw new Error('project-brain runtime dependency is unavailable: getActiveConv');
const pushSubscribe = runtimeScope.pushSubscribe;
if (typeof pushSubscribe !== 'function') throw new Error('project-brain runtime dependency is unavailable: pushSubscribe');
const pushUnsubscribe = runtimeScope.pushUnsubscribe;
if (typeof pushUnsubscribe !== 'function') throw new Error('project-brain runtime dependency is unavailable: pushUnsubscribe');
const showToast = runtimeScope.showToast;
if (typeof showToast !== 'function') throw new Error('project-brain runtime dependency is unavailable: showToast');
/* ===== migrated source: project-brain.js ===== */
/* Signal-driven Project Brain read model.
 *
 * Board, Feed, Status, Attention and Charter are read-only projections of the
 * storage event authority. The only commands owned by this surface are Watch
 * maintenance and versioned Checker registration/execution. There is no
 * claim, block, reopen, handoff, peer inbox or autonomous dispatch UI here.
 */
(function () {
  'use strict';

  var _state = { path: '', tab: 'charter', wired: false, pushKey: '', pushHandler: null };

  function _esc(value) {
    if (typeof escapeHtml === 'function') return escapeHtml(String(value == null ? '' : value));
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }

  function _text(key, fallback) {
    try {
      var value = typeof t === 'function' ? t(key) : '';
      return value && value !== key ? value : fallback;
    } catch (_error) { return fallback; }
  }

  function _path() {
    try {
      var conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
      if (conv && conv.projectPath) return String(conv.projectPath);
    } catch (_error) { /* no active project */ }
    return '';
  }

  function _convId() {
    try {
      var conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
      return conv && conv.id ? String(conv.id) : '';
    } catch (_error) { return ''; }
  }

  function _api() {
    return typeof Api !== 'undefined' && Api.project ? Api.project : null;
  }

  function _time(value) {
    var timestamp = Number(value) || 0;
    if (!timestamp) return '';
    try { return new Date(timestamp).toLocaleString(); }
    catch (_error) { return String(value); }
  }

  function _badge(id, count) {
    var node = document.getElementById(id);
    if (!node) return;
    var value = Math.max(0, Number(count) || 0);
    node.textContent = value > 99 ? '99+' : (value ? String(value) : '');
    node.hidden = !value;
  }

  function _empty(text) {
    return '<div class="pb-board-empty">' + _esc(text) + '</div>';
  }

  function _workRow(item) {
    var paths = Array.isArray(item.changedPaths) ? item.changedPaths : [];
    var meta = [item.status, _time(item.finishedAt || item.startedAt)].filter(Boolean);
    return '<article class="pb-board-card pb-board-' + _esc(item.status || 'active') + '">' +
      '<div class="pb-board-card-title">' + _esc(item.title || item.id || 'Project work') + '</div>' +
      '<div class="pb-board-card-meta">' + _esc(meta.join(' · ')) + '</div>' +
      (paths.length ? '<div class="pb-board-card-desc">' +
        paths.slice(0, 6).map(_esc).join(' · ') + '</div>' : '') +
      (item.resultSummary ? '<div class="pb-board-card-desc">' +
        _esc(item.resultSummary) + '</div>' : '') +
      '</article>';
  }

  function renderBoard(data) {
    var host = document.getElementById('projectBrainBoardBody');
    if (!host) return;
    var active = data && Array.isArray(data.active) ? data.active : [];
    var recent = data && Array.isArray(data.recentOutcomes) ? data.recentOutcomes : [];
    _badge('pbTabCountBoard', active.length);
    host.innerHTML = '<section class="pb-board-lane"><h3>' +
      _esc(_text('projectBrain.activeWork', 'Active')) + '</h3>' +
      (active.length ? active.map(_workRow).join('') :
        _empty(_text('projectBrain.noActiveWork', 'No active work'))) + '</section>' +
      '<section class="pb-board-lane"><h3>' +
      _esc(_text('projectBrain.recentOutcomes', 'Recent outcomes')) + '</h3>' +
      (recent.length ? recent.map(_workRow).join('') :
        _empty(_text('projectBrain.noRecentOutcomes', 'No recent outcomes'))) + '</section>';
  }

  function renderFeed(data) {
    var host = document.getElementById('projectBrainActivityList');
    if (!host) return;
    var events = data && Array.isArray(data.events) ? data.events : [];
    host.innerHTML = events.length ? events.map(function (event) {
      return '<article class="pb-activity-item"><div class="pb-activity-body"><strong>' +
        _esc(event.kind || 'note') + '</strong><p>' + _esc(event.text || '') +
        '</p><time>' + _esc(_time(event.timestamp)) + '</time></div></article>';
    }).join('') : _empty(_text('projectBrain.activityEmpty', 'No important results yet'));
  }

  function _checkerLabel(checker) {
    return (checker.label || checker.checkerId) + ' v' + checker.version;
  }

  function renderCharter(charter, catalog) {
    var host = document.getElementById('projectBrainCharterBody');
    if (!host) return;
    var decisions = charter && Array.isArray(charter.decisions) ? charter.decisions : [];
    var checkers = catalog && Array.isArray(catalog.items) ? catalog.items : [];
    _badge('pbTabCountCharter', decisions.length);
    var checkerRows = checkers.map(function (checker) {
      return '<article class="pb-charter-decision"><div class="pb-decision-text"><strong>' +
        _esc(_checkerLabel(checker)) + '</strong><div><code>' +
        _esc((checker.argv || []).join(' ')) + '</code></div></div>' +
        '<button type="button" class="pb-btn" data-pb-action="run-checker" data-checker-id="' +
        _esc(checker.checkerId) + '" data-checker-version="' + _esc(checker.version) + '">' +
        _esc(_text('projectBrain.runChecker', 'Run')) + '</button></article>';
    }).join('');
    var decisionRows = decisions.map(function (decision) {
      var ref = decision.checkerRef || {};
      var verified = decision.latestVerification || null;
      return '<article class="pb-charter-decision"><div class="pb-decision-text">' +
        _esc(decision.text || '') + '<div class="pb-decision-meta">' +
        _esc((ref.id || '') + (ref.version ? ' v' + ref.version : '')) +
        (verified ? ' · ' + _esc(verified.ok ? 'verified' : 'failed') : '') +
        '</div></div></article>';
    }).join('');
    host.innerHTML = '<section class="pb-charter-section"><h3>' +
      _esc(_text('projectBrain.decisions', 'Executable decisions')) + '</h3>' +
      (decisionRows || _empty(_text('projectBrain.charterEmpty', 'No checker-backed decisions'))) +
      '</section><section class="pb-charter-section"><h3>' +
      _esc(_text('projectBrain.checkers', 'Checker catalog')) + '</h3>' +
      (checkerRows || _empty(_text('projectBrain.noCheckers', 'No checkers registered'))) +
      '<form class="pb-note-editor" id="pbCheckerForm">' +
      '<input name="checkerId" required maxlength="128" placeholder="checker id">' +
      '<input name="label" required maxlength="200" placeholder="label">' +
      '<input name="argv" required placeholder="argv as JSON array">' +
      '<input name="pathGlobs" placeholder="path globs as JSON array">' +
      '<button type="submit" class="pb-btn pb-btn-primary">' +
      _esc(_text('projectBrain.registerChecker', 'Register version')) + '</button></form></section>';
  }

  function renderAttention(data) {
    var host = document.getElementById('projectBrainAttentionBody');
    if (!host) return;
    var items = data && Array.isArray(data.items) ? data.items : [];
    _badge('pbTabCountAttention', items.length);
    host.innerHTML = items.length ? items.map(function (item) {
      return '<article class="pb-attn-card"><div class="pb-attn-title">' +
        _esc(item.kind || 'attention') + '</div><div class="pb-attn-body">' +
        _esc(item.text || '') + '</div><time>' + _esc(_time(item.createdAt)) +
        '</time></article>';
    }).join('') : _empty(_text('projectBrain.attnEmpty', 'Nothing needs you'));
  }

  function _watchRow(item) {
    return '<article class="pb-watch-item"><div class="pb-watch-main"><strong>' +
      _esc(item.kind || 'concern') + '</strong><p>' + _esc(item.text || '') + '</p>' +
      (item.latestResult && item.latestResult.text
        ? '<div class="pb-watch-verdict">' + _esc(item.latestResult.text) + '</div>' : '') +
      '</div><div class="pb-watch-actions">' +
      '<button type="button" data-pb-action="resolve-watch" data-item-id="' + _esc(item.id) + '">' +
      _esc(item.status === 'resolved' ? 'Reopen' : 'Resolve') + '</button>' +
      '<button type="button" data-pb-action="delete-watch" data-item-id="' + _esc(item.id) + '">' +
      _esc(_text('common.delete', 'Delete')) + '</button></div></article>';
  }

  function renderStatus(status, watch) {
    var host = document.getElementById('projectBrainStatusBody');
    if (!host) return;
    status = status || {};
    var items = watch && Array.isArray(watch.items) ? watch.items : [];
    host.innerHTML = '<div class="pb-status-evidence">' +
      '<span class="pb-status-ev-chip">' + _esc((status.activeCount || 0) + ' active') + '</span>' +
      '<span class="pb-status-ev-chip">' + _esc((status.recentOutcomeCount || 0) + ' recent') + '</span>' +
      '<span class="pb-status-ev-chip">' + _esc((status.attentionCount || 0) + ' attention') + '</span>' +
      '<span class="pb-status-ev-chip">' + _esc((status.checkerCount || 0) + ' checkers') + '</span></div>' +
      '<section id="pbWatchSection"><h3>' + _esc(_text('projectBrain.watchHead', 'Watch')) + '</h3>' +
      '<form class="pb-watch-composer" id="pbWatchForm"><select name="kind">' +
      '<option value="concern">Concern</option><option value="question">Question</option>' +
      '<option value="goal">Goal</option></select><input name="text" required maxlength="4000" ' +
      'placeholder="What should the project keep visible?">' +
      '<button type="submit">' + _esc(_text('common.add', 'Add')) + '</button></form>' +
      (items.length ? items.map(_watchRow).join('') :
        _empty(_text('projectBrain.watchEmpty', 'Nothing on Watch'))) + '</section>';
  }

  function _reportFailure(error) {
    var message = error && error.message ? error.message : String(error || 'Project Brain request failed');
    if (typeof showToast === 'function') showToast(message, 'error');
    else if (typeof console !== 'undefined') console.warn('[ProjectBrain]', error);
  }

  function refreshAll(path) {
    path = path || _state.path || _path();
    var api = _api();
    if (!api || !path) return Promise.resolve();
    _state.path = path;
    return Promise.all([
      api.board(path), api.feed(path, 0), api.charter(path), api.brainCheckers(path),
      api.brainAttention(path), api.brainStatus(path), api.brainWatchList(path),
    ]).then(function (values) {
      if (path !== _state.path) return;
      renderBoard(values[0] || {});
      renderFeed(values[1] || {});
      renderCharter(values[2] || {}, values[3] || {});
      renderAttention(values[4] || {});
      renderStatus(values[5] || {}, values[6] || {});
    }).catch(_reportFailure);
  }

  function _selectTab(name) {
    _state.tab = name;
    document.querySelectorAll('#projectBrainTabs [data-pb-tab]').forEach(function (node) {
      var selected = node.getAttribute('data-pb-tab') === name;
      node.classList.toggle('pb-tab-active', selected);
      node.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    document.querySelectorAll('.project-brain-columns [data-pb-panel]').forEach(function (node) {
      node.classList.toggle('pb-tab-panel-active', node.getAttribute('data-pb-panel') === name);
    });
    if (name === 'integration' && runtimeScope.ProjectBrainIntegration) {
      runtimeScope.ProjectBrainIntegration.refreshIntegration(_state.path);
    }
  }

  function _wire() {
    if (_state.wired) return;
    var tabs = document.getElementById('projectBrainTabs');
    var panel = document.querySelector('.project-brain-panel');
    if (!tabs || !panel) return;
    tabs.addEventListener('click', function (event) {
      var button = event.target.closest('[data-pb-tab]');
      if (button) _selectTab(button.getAttribute('data-pb-tab'));
    });
    panel.addEventListener('click', function (event) {
      var button = event.target.closest('[data-pb-action]');
      if (!button) return;
      var api = _api();
      if (!api || !_state.path) return;
      var action = button.getAttribute('data-pb-action');
      button.disabled = true;
      var operation;
      if (action === 'run-checker') {
        operation = api.brainCheckerRun(
          _state.path, button.getAttribute('data-checker-id'),
          Number(button.getAttribute('data-checker-version')) || 0, '');
      } else if (action === 'resolve-watch') {
        operation = api.brainWatchUpdate(_state.path, button.getAttribute('data-item-id'), {
          status: button.textContent.trim() === 'Reopen' ? 'active' : 'resolved',
        });
      } else if (action === 'delete-watch') {
        operation = api.brainWatchDelete(_state.path, button.getAttribute('data-item-id'));
      }
      if (!operation) { button.disabled = false; return; }
      Promise.resolve(operation).then(function () { return refreshAll(_state.path); })
        .catch(_reportFailure).finally(function () { button.disabled = false; });
    });
    panel.addEventListener('submit', function (event) {
      var form = event.target;
      var api = _api();
      if (!api || !_state.path) return;
      if (form.id === 'pbWatchForm') {
        event.preventDefault();
        var data = new FormData(form);
        Promise.resolve(api.brainWatchAdd(
          _state.path, data.get('kind'), data.get('text'), _convId()))
          .then(function () { form.reset(); return refreshAll(_state.path); })
          .catch(_reportFailure);
      } else if (form.id === 'pbCheckerForm') {
        event.preventDefault();
        try {
          var fields = new FormData(form);
          var existing = form.querySelector('[name="checkerId"]').value.trim();
          var catalogHost = document.getElementById('projectBrainCharterBody');
          var versions = catalogHost ? catalogHost.querySelectorAll(
            '[data-checker-id="' + CSS.escape(existing) + '"]') : [];
          var definition = {
            checkerId: existing,
            version: versions.length + 1,
            label: fields.get('label'),
            argv: JSON.parse(fields.get('argv')),
            cwd: '.',
            pathGlobs: fields.get('pathGlobs') ? JSON.parse(fields.get('pathGlobs')) : ['**'],
            timeoutMs: 120000,
            enabled: true,
          };
          Promise.resolve(api.brainCheckerRegister(_state.path, definition))
            .then(function () { form.reset(); return refreshAll(_state.path); })
            .catch(_reportFailure);
        } catch (error) { _reportFailure(error); }
      }
    });
    _state.wired = true;
  }

  function _subscribe(path) {
    if (_state.pushHandler && typeof pushUnsubscribe === 'function') {
      try { pushUnsubscribe('project', _state.pushKey, _state.pushHandler); }
      catch (_error) { /* best effort */ }
      _state.pushHandler = null;
      _state.pushKey = '';
    }
    if (typeof pushSubscribe !== 'function' || !path) return;
    try {
      _state.pushKey = projectKeyHash(path);
      _state.pushHandler = function (event) {
        if (event && (event.type === 'project_brain_changed' || event.type === 'path_overlap')) {
          refreshAll(path);
        }
      };
      pushSubscribe('project', _state.pushKey, _state.pushHandler);
    } catch (_error) { /* projection reads remain available */ }
  }

  function openProjectBrain(opts) {
    var path = opts && opts.path ? String(opts.path) : _path();
    var overlay = document.getElementById('projectBrainOverlay');
    if (!overlay) return;
    overlay.hidden = false;
    _wire();
    if (!path) {
      ['projectBrainAttentionBody', 'projectBrainCharterBody', 'projectBrainBoardBody',
        'projectBrainActivityList', 'projectBrainStatusBody'].forEach(function (id) {
          var node = document.getElementById(id);
          if (node) node.innerHTML = _empty(_text('projectBrain.noProject', 'Attach a project first'));
        });
      return;
    }
    _state.path = path;
    _subscribe(path);
    refreshAll(path);
    _selectTab(opts && opts.tab ? opts.tab : _state.tab);
  }

  function closeProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay) overlay.hidden = true;
    if (_state.pushHandler && typeof pushUnsubscribe === 'function') {
      try { pushUnsubscribe('project', _state.pushKey, _state.pushHandler); }
      catch (_error) { /* best effort */ }
      _state.pushHandler = null;
      _state.pushKey = '';
    }
  }

  function toggleProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay && !overlay.hidden) closeProjectBrain();
    else openProjectBrain();
  }

  /* Synchronous SHA-1 used only for the opaque project push channel key. */
  function projectKeyHash(value) {
    function rotl(number, shift) { return (number << shift) | (number >>> (32 - shift)); }
    var bytes = unescape(encodeURIComponent(String(value || ''))), words = [];
    for (var i = 0; i < bytes.length; i++) words[i >> 2] |= bytes.charCodeAt(i) << ((3 - i % 4) * 8);
    var bitLength = bytes.length * 8;
    words[bitLength >> 5] |= 0x80 << (24 - bitLength % 32);
    words[((bitLength + 64 >> 9) << 4) + 15] = bitLength;
    var h0 = 1732584193, h1 = -271733879, h2 = -1732584194, h3 = 271733878, h4 = -1009589776;
    for (var block = 0; block < words.length; block += 16) {
      var a = h0, b = h1, c = h2, d = h3, e = h4, schedule = [];
      for (var round = 0; round < 80; round++) {
        schedule[round] = round < 16 ? (words[block + round] | 0)
          : rotl(schedule[round - 3] ^ schedule[round - 8] ^ schedule[round - 14] ^ schedule[round - 16], 1);
        var f = round < 20 ? ((b & c) | (~b & d)) : round < 40 ? (b ^ c ^ d)
          : round < 60 ? ((b & c) | (b & d) | (c & d)) : (b ^ c ^ d);
        var k = round < 20 ? 1518500249 : round < 40 ? 1859775393 : round < 60 ? -1894007588 : -899497514;
        var temp = (rotl(a, 5) + f + e + k + schedule[round]) | 0;
        e = d; d = c; c = rotl(b, 30); b = a; a = temp;
      }
      h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0;
      h3 = (h3 + d) | 0; h4 = (h4 + e) | 0;
    }
    function hex(number) { var out = ''; for (var j = 7; j >= 0; j--) out += ((number >>> (j * 4)) & 15).toString(16); return out; }
    return (hex(h0) + hex(h1) + hex(h2) + hex(h3) + hex(h4)).slice(0, 16);
  }

  runtimeScope.ProjectBrain = {
    renderBoard: renderBoard,
    renderFeed: renderFeed,
    renderCharter: renderCharter,
    renderAttention: renderAttention,
    renderStatus: renderStatus,
    refreshAll: refreshAll,
    _reportFailure: _reportFailure,
  };
  runtimeScope.openProjectBrain = openProjectBrain;
  runtimeScope.closeProjectBrain = closeProjectBrain;
  runtimeScope.toggleProjectBrain = toggleProjectBrain;
})();
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

// BEGIN GENERATED LAZY RUNTIME PORTS — project-brain
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — project-brain
// END GENERATED LAZY RUNTIME ACTIONS
