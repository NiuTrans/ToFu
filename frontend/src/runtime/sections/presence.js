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
    // The router home and the Watchdog location are not projects: the main
    // coordinator's card stands in that slot instead of a project Coordinator row.
    if (sidebarOnly && isRouterManaged(root)) return null;
    // Both surfaces read through one join: identical reads in one task share a request.
    function sharedRead(endpoint, args, signal) {
      return joinRead([endpoint].concat(args), signal, function (shared) {
        return api[endpoint].apply(api, args.concat([shared]));
      });
    }
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
            var stopReconnect = typeof pushOnReconnect === 'function' ? pushOnReconnect(refresh) : null;
            return function () { pushUnsubscribe('project', '*', refresh); stopReconnect?.(); };
          },
          binding: function (path, signal) { return sharedRead('coordination', [path], signal); },

          workerConversations: function (path, cursor, signal) { return sharedRead('workerConversations', [path, cursor], signal); },
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
            list: function (path, commandId, options, signal) { return sharedRead('coordinationAssignments', [path, commandId, options], signal); },
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
            read: function (path, signal) { return sharedRead('coordinatorReports', [path], signal); },
            markRead: function (body) { return api.markCoordinatorReportRead(body); },
          },
          budget: {
            read: function (path, signal) { return sharedRead('coordinatorBudget', [path], signal); },
          },
          // The router home's control lives in the main coordinator's header instead;
          // the Watchdog never delegates, so it has no Worker model and no task board.
          workerModel: sidebarOnly || isRouterManaged(root) ? undefined : routerWorkerModelPorts,
          progress: isRouterWatchdog(root) ? undefined : {
            read: function (path, signal) { return sharedRead('coordinatorProgress', [path], signal); },
          },
          messages: function (path, id, beforeId) { return api.coordinationMessages(path, id, beforeId); },
          board: function (path, signal) { return sharedRead('board', [path], signal); },
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
  // A conversation is the Global Router when its project is the router home
  // the Global Router owner last read; the binding there is the identity.
  function isRouterHome(path) {
    var home = globalRouter.home();
    return !!home && norm(home) === norm(path);
  }
  function isRouterWatchdog(path) {
    var location = routerWatchdog.location();
    return !!location && norm(location) === norm(path);
  }
  function isRouterManaged(path) { return isRouterHome(path) || isRouterWatchdog(path); }
  // Worker model choices are the composer model picker's catalog and policy.
  var routerWorkerModelPorts = {
    read: function (path, signal) { return Api.project.routerWorkerModel(path, signal); },
    write: function (path, model) { return Api.project.setRouterWorkerModel(path, model); },
    catalog: {
      models: function () { return (typeof _registeredModels !== 'undefined' && _registeredModels) || []; },
      visible: function (model) {
        if (typeof _hiddenModels !== 'undefined' && _hiddenModels.has(model.model_id)) return false;
        return typeof runtimeScope.isChatModel !== 'function' || runtimeScope.isChatModel(model);
      },
      compare: function (left, right) {
        return typeof _compareModelsByDisplayName === 'function' ? _compareModelsByDisplayName(left, right) : 0;
      },
      label: function (id) { return typeof _modelShortName === 'function' ? _modelShortName(id) : id; },
    },
  };
  var host = document.getElementById('presenceStrip');
  var list = document.getElementById('convList');
  if (!host || !list) return;
  var joinRead = createSameTaskReadJoin();
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
  // Narrow bridge to the typed Global Router and Watchdog owners
  // (features/global-router.ts, features/router-watchdog.ts); delete with the
  // retained sidebar and conversation navigation. The main coordinator's card
  // takes the leader slot wherever the list has no project of its own.
  var routerEntryHost = document.createElement('div');
  routerEntryHost.id = 'globalRouterEntry';
  column.insertBefore(routerEntryHost, sidebar);
  var routerHeaderHost = document.createElement('div');
  var watchdogHeaderHost = document.createElement('div');
  host.before(routerHeaderHost, watchdogHeaderHost);
  function openRouterConversation(id) { if (typeof loadConversation === 'function') loadConversation(id); }
  function activeRouterConversationId() {
    return String((typeof getActiveConv === 'function' && getActiveConv()?.id)
      || (typeof activeConvId !== 'undefined' ? activeConvId : '') || '');
  }
  function notifyRouterError(message) { if (typeof showToast === 'function') showToast(message, 'error'); }
  // A new router identity changes which rows the list hides; the list render refreshes presence.
  function routerIdentityChanged() {
    if (typeof renderConversationList === 'function') renderConversationList();
    else runtimeScope.presenceRefresh?.();
  }
  var routerWatchdog = createRouterWatchdogEntry({
    read: function (signal) { return Api.project.routerWatchdog(signal); },
    ensure: function () { return Api.project.ensureRouterWatchdog(); },
    configureReview: function (request) { return Api.project.configureRouterWatchdogReview(request); },
    open: openRouterConversation,
    back: function () { if (globalRouter.conversationId()) openRouterConversation(globalRouter.conversationId()); },
    activeConversationId: activeRouterConversationId,
    notifyError: notifyRouterError,
    changed: routerIdentityChanged,
  }, { header: watchdogHeaderHost });
  // "Act for me": the owner's grant of their operations to the main coordinator (docs/ROUTER.md R9).
  var routerOwnerDelegation = createRouterOwnerDelegation({
    read: function (signal) { return Api.project.routerOwnerDelegation(signal); },
    set: function (granted) { return Api.project.setRouterOwnerDelegation(granted); },
  });
  var globalRouter = createGlobalRouterEntry({
    read: function (signal) { return Api.project.globalRouter(signal); },
    ensure: function () { return Api.project.ensureGlobalRouter(); },
    open: openRouterConversation,
    activeConversationId: activeRouterConversationId,
    notifyError: notifyRouterError,
    changed: routerIdentityChanged,
    workerModel: routerWorkerModelPorts,
    instructions: {
      list: function (cursor, signal) { return Api.project.routerInstructions(cursor, signal); },
      pause: function (id) { return Api.project.pauseRouterInstruction(id); },
      resume: function (id) { return Api.project.resumeRouterInstruction(id); },
      cancel: function (id) { return Api.project.cancelRouterInstruction(id); },
    },
  }, { sidebar: routerEntryHost, header: routerHeaderHost,
    controls: [routerOwnerDelegation.settings, routerWatchdog.settings] });
  // Other surfaces (the studio's compute-stall dialog) ensure the Watchdog through its
  // owner, so the header and composer steering adopt the binding they open.
  runtimeScope.ensureRouterWatchdogConversation = function () { return routerWatchdog.ensure(); };
  function sidebarHasProject() {
    var root = sidebarPath();
    return !!root && !isRouterManaged(root);
  }
  /* A bound Coordinator's composer steers its live reply without asking
   * (_promptInjectMode). Each owner names one from a server binding: the open
   * chat's project, the browsed folder's project, the Global Router and the
   * Watchdog, so browsing another folder never changes the answer. */
  runtimeScope.isCoordinatorConversation = function (id) {
    return !!id && (id === surface.coordinatorId() || id === sidebarSurface.coordinatorId()
      || id === globalRouter.conversationId() || id === routerWatchdog.conversationId());
  };
  // Reached from the main coordinator's card and settings, never as list rows.
  runtimeScope.routerConversationIds = function () {
    return [globalRouter.conversationId(), routerWatchdog.conversationId()].filter(Boolean);
  };
  runtimeScope.presenceRefresh = function () {
    routerEntryHost.hidden = sidebarHasProject();
    // The router home and the Watchdog location are internal coordination
    // homes, not projects the owner attaches, detaches or edits.
    var projectBar = document.getElementById('projectBar');
    if (projectBar) projectBar.hidden = isRouterManaged(projectPath());
    surface.refresh(); sidebarSurface.refresh(); globalRouter.refresh(); routerWatchdog.refresh();
  };
  // Remove with the retained catalog bridge; grouping policy, rendering and
  // provenance live in the typed group and existing conversation owners.
  var coordinatorConversationGroups = createCoordinatorConversationGroups({
    listHost: list,
    pinned: function () {
      var pins = [];
      var project = sidebar.querySelector('[data-coordinator-id]')?.dataset.coordinatorId;
      if (project) pins.push({ id: project, path: sidebarPath(), host: sidebar });
      // Both router conversations stay reserved, so neither becomes a replaced
      // Coordinator's row. Only the main coordinator groups its work, and only
      // beneath its card while the card is shown; the Watchdog never delegates.
      if (globalRouter.conversationId()) pins.push({
        id: globalRouter.conversationId(), path: routerEntryHost.hidden ? '' : globalRouter.home() || '', host: routerEntryHost,
      });
      if (routerWatchdog.conversationId()) pins.push({ id: routerWatchdog.conversationId(), path: '', host: routerEntryHost });
      return pins;
    },
    historySettled: function () { return !sidebar.querySelector('.coordination-leader[aria-busy="true"]'); },
    projectPath: function (c) { return _getConvProjectPath(c); },
    rows: function () { return conversations; },
    readPage: function (path, cursor, signal) { return Api.project.workerConversations(path, cursor, signal); },
    adoptCoordinator: function (id, path) {
      if (!id || !path) return;
      mergeConversationCatalogRows([{ id: id, settings: { projectPath: path } }]);
    },
    adoptPage: function (page) {
      for (const item of page.conversations) {
        mergeConversationCatalogRows([{
          id: item.conversationId, title: item.title, msgCount: item.turnCount,
          createdAt: item.createdAt, updatedAt: item.updatedAt, busy: item.busy === true,
          settings: { _coordinatorSource: item.coordinatorId, folderId: item.folderId },
        }]);
        var local = conversations.find(function (row) { return row.id === item.conversationId; });
        if (local) local._localOnly = false;
      }
    },
    open: function (id) { if (typeof loadConversation === 'function') loadConversation(id); },
    renderRow: function (c) {
      return _buildConvItemHTML(c, escapeHtml(_conversationDisplayTitle(c.title, t('chat.newConversation'))), '');
    },
    activeConversationId: activeRouterConversationId,
    refresh: function () { renderConversationList(); },
    refreshActive: function () {
      if (_swapActiveConvItem(activeRouterConversationId())) renderConversationList();
    },
    isRunning: function (c) {
      var flags = _convStatusFlags(c);
      return flags.streaming || flags.waiting || Boolean(flags.background && !flags.background.queuedOnly);
    },
    status: function (c) {
      var flags = _convStatusFlags(c);
      if (flags.streaming || flags.waiting || flags.background) return 'running';
      if (flags.errored) return 'failed';
      if (flags.incomplete) return 'cancelled';
      // An unhydrated shell has no Turn evidence; never present it as finished.
      return (runtimeScope.ConversationTurnRead?.ordered?.(c) || []).length ? 'done' : null;
    },
  });
  runtimeScope.coordinatorConversationGroups = coordinatorConversationGroups;
  const refreshPresence = runtimeScope.presenceRefresh;
  runtimeScope.presenceRefresh = function () {
    refreshPresence();
    coordinatorConversationGroups.refresh();
  };
  window.addEventListener('pagehide', function (event) {
    if (!event.persisted) {
      surface.destroy(); sidebarSurface.destroy(); globalRouter.destroy(); routerWatchdog.destroy();
      coordinatorConversationGroups.destroy();
    }
  });
  runtimeScope.presenceRefresh();
})();
