/* ===== migrated source: presence.js ===== */
/* Project collaboration bar derived only from the Project Brain projection.
 * Presence/peer state is intentionally absent: work ownership never transfers,
 * overlap advice is execution-local, and the bar is a projection shortcut.
 */
(function wireProjectProjectionBar() {
  'use strict';
  if (typeof pushSubscribe !== 'function') return;
  if (typeof createPresenceSummaryController !== 'function') return;
  if (runtimeScope.__presenceWired) return;
  runtimeScope.__presenceWired = true;

  var lastFingerprint = '';
  var refetchTimer = null;

  function norm(value) { return String(value || '').replace(/[/\\]+$/, ''); }
  function projectPath() {
    try {
      var conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
      var path = conv
        ? (typeof _getConvProjectPath === 'function'
          ? _getConvProjectPath(conv) : conv.projectPath)
        : '';
      if (!path && typeof projectState !== 'undefined' && projectState?.active) {
        path = projectState.path || '';
      }
      return norm(path);
    } catch (_error) { return ''; }
  }
  function esc(value) { return escapeHtml(String(value == null ? '' : value)); }
  function text(key, params, fallback) {
    try {
      var value = typeof t === 'function' ? t(key, params) : '';
      return value && value !== key ? value : fallback;
    } catch (_error) { return fallback; }
  }

  var controller = createPresenceSummaryController({
    currentScope: function () {
      var root = projectPath();
      return root ? { root: root, selfConversationId: '' } : null;
    },
    fetchSummary: function (root) {
      var api = typeof Api !== 'undefined' ? Api.project : null;
      return api && typeof api.brainSummary === 'function'
        ? api.brainSummary(root) : null;
    },
    onSummaryChanged: render,
    onError: function () { /* bar degrades to hidden */ },
  });

  function segments(summary) {
    var status = summary?.status || {};
    var attention = Array.isArray(summary?.attention?.items)
      ? summary.attention.items.length : Number(status.attentionCount || 0);
    var active = Number(status.activeCount || 0);
    var recent = Number(status.recentOutcomeCount || 0);
    var values = [];
    if (attention > 0) values.push({
      className: 'collab-seg-needsyou',
      label: text('collab.needsYou', { n: attention }, attention + ' need you'),
    });
    if (active > 0) values.push({
      className: 'collab-seg-progress',
      label: text('projectBrain.activeWork', null, 'Active') + ': ' + active,
    });
    if (recent > 0) values.push({
      className: 'collab-seg-outcomes',
      label: text('projectBrain.recentOutcomes', null, 'Recent outcomes') + ': ' + recent,
    });
    return values;
  }

  function render() {
    var host = document.getElementById('presenceStrip');
    if (!host) return;
    var root = projectPath();
    if (!root) {
      host.hidden = true;
      host.innerHTML = '';
      lastFingerprint = '';
      return;
    }
    var summary = controller.summaryFor(root, '');
    var rows = segments(summary);
    if (!rows.length) {
      host.hidden = true;
      host.innerHTML = '';
      lastFingerprint = '';
      return;
    }
    var body = rows.map(function (row) {
      return '<span class="collab-seg ' + row.className + '">' +
        esc(row.label) + '</span>';
    }).join('<span class="collab-sep">·</span>');
    var html = '<button type="button" class="collab-bar-inner" ' +
      'data-testid="collab-bar" title="' +
      esc(text('collab.openBrain', null, 'Open Project Brain')) + '">' +
      '<span class="collab-label">' +
      esc(text('collab.project', null, 'Project')) + '</span>' +
      '<span class="collab-sep">·</span>' + body + '</button>';
    var fingerprint = root + '|' + html;
    if (fingerprint === lastFingerprint) return;
    lastFingerprint = fingerprint;
    host.innerHTML = html;
    host.hidden = false;
    host.querySelector('.collab-bar-inner')?.addEventListener('click', function () {
      if (typeof runtimeScope.openProjectBrain === 'function') {
        var attention = Number(summary?.status?.attentionCount || 0);
        runtimeScope.openProjectBrain({ tab: attention ? 'attention' : 'board' });
      }
    });
  }

  function scheduleRefresh() {
    if (refetchTimer) clearTimeout(refetchTimer);
    refetchTimer = setTimeout(function () {
      refetchTimer = null;
      controller.refresh();
    }, 300);
  }

  pushSubscribe('project', '*', scheduleRefresh);
  runtimeScope.presenceRefresh = function () {
    if (projectPath()) controller.refresh();
    render();
  };
  runtimeScope.CollabBar = {
    _render: render,
    _setSummary: function (root, summary) {
      controller.adoptSummary(root, '', summary);
    },
  };
  runtimeScope.presenceRefresh();
})();
