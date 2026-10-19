/* ===== migrated source: presence.js ===== */
/* Narrow retained adapter for the typed conversation-first project surface.
 * Delete with retained project/catalog navigation; no presentation state here. */
(function wireProjectProjectionBar() {
  if (runtimeScope.__presenceWired) return;
  runtimeScope.__presenceWired = true;
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

  function sidebarPath() {
    var folder = typeof getFolders === 'function' && typeof getActiveFolderId === 'function'
      ? getFolders().find(function (item) { return item.id === getActiveFolderId(); }) : null;
    return folder ? norm(folder.projectPaths?.[0] || folder.projectPath || '') : projectPath();
  }
  function scope(sidebarOnly) {
    var resolvePath = sidebarOnly ? sidebarPath : projectPath;
    var root = resolvePath();
    var conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
    if (root !== projectPath()) conv = null;
    var api = typeof Api !== 'undefined' ? Api.project : null;
    if (!root || !api) return null;
    return {
          path: root, currentConversationId: conv?.id || '', currentPath: resolvePath,

          currentConversation: function () { return root === projectPath() ? (getActiveConv()?.id || '') : ''; },
          conversations: function () {
            return conversations.filter(function (item) {
              return !item._localOnly && !item.archived && !item.readOnly
                && norm(typeof _getConvProjectPath === 'function' ? _getConvProjectPath(item) : item.projectPath) === root;
            }).map(function (item) { return { id: item.id, title: item.title || item.id,
              turnCount: item._serverTurnCount ?? runtimeScope.ConversationTurnRead?.ordered?.(item)?.length ?? null,
              coordinatorId: item._coordinatorSource || null }; });
          },
          subscribe: function (refresh) {
            pushSubscribe('project', '*', refresh);
            return function () { pushUnsubscribe('project', '*', refresh); };
          },
          binding: function (path) { return api.coordination(path); },

          workerConversations: function (path, cursor) { return api.workerConversations(path, cursor); },
          dispatch: {
            recipients: function () {
              return conversations.filter(function (item) {
                return !item._localOnly && !item.archived && !item.readOnly && item.id !== conv?.id
                  && norm(typeof _getConvProjectPath === 'function' ? _getConvProjectPath(item) : item.projectPath) === root;
              }).map(function (item) { return { id: item.id, title: item.title || item.id }; });
            },
            humanTurns: function () {
              var state = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv?.id)?.getState?.();
              return Object.values(state?.turnsById || {}).filter(function (turn) {
                return turn?.actor === 'human' && turn.projectionRevision > 0;
              }).map(function (turn) {
                return { actor: 'human', turnId: turn.turnId, projectionRevision: turn.projectionRevision,
                  promptPreview: typeof turn.projection?.content === 'string' ? turn.projection.content.slice(0, 120) : '' };
              });
            },
            commandId: function () { return api.newAssignmentCommandId(); },
            list: function (path, commandId, options) { return api.coordinationAssignments(path, commandId, options); },
            grant: function (request, commandId) { return api.grantAssignment(request, commandId); },
            dispatch: function (request) { return api.dispatchAssignment(request); },
            revoke: function (request) { return api.revokeAssignment(request); },
            review: function (request) { return api.reviewAssignment(request); },

            publish: function (request) { return api.publishAssignment(request); },
          },

          scopeChanges: {
            list: async function (path) { return (await api.coordinationScopeChanges(path)).scopeChanges; },
            apply: function (body, key) { return api.applyScopeChange(body, key); },
            retry: function (body, key) { return api.retryScopeChange(body, key); },
          },
          responsibilities: {
            list: async function (path) { return (await api.coordinationResponsibilities(path)).responsibilities; },
            create: function (body, key) { return api.createResponsibility(body, key); },
            pause: function (body, key) { return api.pauseResponsibility(body, key); },
            cancel: function (body, key) { return api.cancelResponsibility(body, key); },
            reconcile: function (body, key) { return api.reconcileResponsibility(body, key); },
          },
          sharedFiles: {
            list: function (path, cursor) { return api.coordinationSharedFiles(path, cursor); },
            read: function (_path, body) { return api.readSharedFile(body, api.newAssignmentCommandId()); },

            register: function (body, key) { return api.registerSharedFile(body, key); },
            refresh: function (body, key) { return api.refreshSharedFile(body, key); },
          },

          reports: {
            read: function (path) { return api.coordinatorReports(path); },
            markRead: function (body) { return api.markCoordinatorReportRead(body); },
          },
          budget: {
            read: function (path) { return api.coordinatorBudget(path); },
            update: function (body) { return api.updateCoordinatorBudget(body); },
          },
          messages: function (path, id, beforeId) { return api.coordinationMessages(path, id, beforeId); },
          board: function (path) { return api.board(path); },
          bind: function (path, id, revision) { return api.bindCoordinator(path, id, revision); },

          create: function (path, revision) {
            var folderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : '';
            return api.createCoordinator(path, folderId, revision);
          },
          navigate: function (id) { if (typeof loadConversation === 'function') loadConversation(id); },

          diagnose: function (id) {
            var prepared = _projectConversationNavigation.prepareDraft(
              t('projectCoordination.diagnosePrompt', { id: id, path: root }), {
                hasDraft: _hasProjectComposerDraft,
                startDraft: function () { newChat(); _saveConvProjectPath(root, [], [root]); },
                writeText: function (text) {
                  var input = document.getElementById('userInput');
                  input.value = text;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.focus();
                },
              });
            if (!prepared) showToast(t('projectCoordination.diagnoseDraftConflict'), 'warning');
          },
          openEvidence: function () { runtimeScope.openProjectBrain?.({ tab: 'integration' }); },

    };
  }
  var host = document.getElementById('presenceStrip');
  var list = document.getElementById('convList');
  if (!host || !list) return;
  var sidebar = document.createElement('div');
  sidebar.id = 'projectCoordinatorEntry';
  var column = document.createElement('div');
  column.className = 'coordination-conversation-column';
  list.before(column);
  column.append(sidebar, list);
  host.hidden = false;
  function createSurface(sidebarOnly) {
    return createProjectCoordinationSurface({
    sidebar: sidebarOnly ? sidebar : document.createElement('div'),
    canvas: sidebarOnly ? document.createElement('div') : host,
    mode: sidebarOnly ? 'sidebar' : 'canvas',
    scope: function () { return scope(sidebarOnly); }, sidebarPath: sidebarPath,

    changed: function () { if (typeof renderConversationList === 'function') renderConversationList(); },
    canCreate: function () { return typeof getActiveFolderId === 'function' && !!getActiveFolderId(); },
    create: async function (ports, binding) {
      var folderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : '';

      var navigationCurrent = _projectConversationNavigation.checkpoint();
      var created = await Api.project.createCoordinator(ports.path, folderId || '', binding.revision);
      if (!navigationCurrent() || folderId !== getActiveFolderId()
          || norm(ports.currentPath()) !== norm(ports.path)) return;
      if (created.availability === 'available') ports.navigate(created.conversationId);
    },
    details: function (ports) {
      openProjectCoordinationWorkspace(ports).then(function () { surface.refresh(true); sidebarSurface.refresh(true); }).catch(function (error) {
        if (typeof showToast === 'function') showToast(error.message || String(error), 'error');
      });
    },
  });
  }
  var surface = createSurface(false);
  var sidebarSurface = createSurface(true);
  runtimeScope.presenceRefresh = function () { surface.refresh(); sidebarSurface.refresh(); };
  window.addEventListener('pagehide', function (event) { if (!event.persisted) { surface.destroy(); sidebarSurface.destroy(); } });
  runtimeScope.presenceRefresh();
})();
