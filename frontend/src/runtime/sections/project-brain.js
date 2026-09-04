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
