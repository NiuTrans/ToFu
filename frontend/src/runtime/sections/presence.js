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
    var active = Number(status.activeCount || 0);
    var recent = Number(status.recentOutcomeCount || 0);
    var values = [];
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
    var label = [text('projectCoordination.title', null, 'Project workspace')]
      .concat(rows.map(function (row) { return row.label; })).join(' · ');
    var fingerprint = root + '|' + label;
    if (fingerprint === lastFingerprint) return;
    lastFingerprint = fingerprint;
    host.replaceChildren();
    host.hidden = false;

    var coordination = createProjectCoordinationButton({
      label: label,
      size: 'sm', variant: 'ghost',
      onClick: function () {
        var conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
        var api = typeof Api !== 'undefined' ? Api.project : null;
        if (!api || !root) return;
        openProjectCoordinationWorkspace({
          path: root, currentConversationId: conv?.id || '', currentPath: projectPath,

          currentConversation: function () { return getActiveConv()?.id || ''; },
          binding: function (path) { return api.coordination(path); },

          messages: function (path, id, beforeId) { return api.coordinationMessages(path, id, beforeId); },
          board: function (path) { return api.board(path); },
          bind: function (path, id, revision) { return api.bindCoordinator(path, id, revision); },
          navigate: function (id) { if (typeof loadConversation === 'function') loadConversation(id); },
          openEvidence: function () { runtimeScope.openProjectBrain?.({ tab: 'integration' }); },
        }).catch(function (error) {
          if (typeof showToast === 'function') showToast(error.message || String(error), 'error');
        });
      },
    });
    coordination.setAttribute('data-testid', 'collab-bar');
    host.appendChild(coordination);
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
