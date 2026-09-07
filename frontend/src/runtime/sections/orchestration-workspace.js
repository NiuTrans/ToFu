/* ===== migrated source: orchestration-workspace.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-workspace.js — Studio workspace coordinator

   Owns builtin/layout authoring and popup composition. Persisted definition
   save/load/delete semantics live in orchestration-workspace-persistence.js;
   saved-flow DOM lives in orchestration-store-browser.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationWorkspaceController(options) {
  options = options || {};
  var popupMenus = options.popupMenus;
  var workspaceSession = options.workspaceSession
    || createOrchestrationWorkspaceSessionPort(options);
  var authoringRequest = options.authoringRequest ||
    createOrchestrationWorkspaceRequestClient({
      api: options.api,
      normalizeBuiltin: options.normalizeBuiltin,
      normalizeLayout: options.normalizeLayout,
    });
  var definitionRequest = options.definitionRequest ||
    createOrchestrationDefinitionRequestClient({
      api: options.api,
      normalizeList: options.normalizeList,
      normalizeRead: options.normalizeRead,
      normalizeSave: options.normalizeSave,
      normalizeDelete: options.normalizeDelete,
      definitionWriteContract: options.definitionWriteContract,
      definitionListContract: options.definitionListContract,
      definitionEntryContract: options.definitionEntryContract,
    });
  var storeBrowser = null;
  var persistence = createOrchestrationWorkspacePersistence(Object.assign(
    {},
    options,
    {
      definitionRequest: definitionRequest,
      workspaceSession: workspaceSession,
      closeStore: function () {
        return storeBrowser ? storeBrowser.close() : false;
      },
      refreshStore: function () {
        return storeBrowser ? storeBrowser.open(true) : Promise.resolve([]);
      },
    }
  ));

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _toast(message, error) {
    if (typeof options.toast === 'function') options.toast(message, error);
  }

  function _applyDefinitionResult(definition, id, opts) {
    return workspaceSession.applyDefinitionResult(definition, id, opts);
  }

  function loadFromStore(id, opts) {
    return persistence.load(id, opts);
  }

  function deleteFromStore(id, event, listedUpdatedAt) {
    if (event) event.stopPropagation();
    return persistence.remove(id, listedUpdatedAt);
  }

  storeBrowser = createOrchestrationStoreBrowser({
    document: options.document,
    popupMenus: popupMenus,
    definitions: definitionRequest,
    currentId: persistence.currentId,
    now: options.now,
    translate: options.translate,
    escape: options.escape,
    icons: options.icons,
    onError: options.onError,
    onLoad: loadFromStore,
    onDelete: deleteFromStore,
  });

  function popupIsOpen(menuId) {
    return !!popupMenus && popupMenus.isOpen(menuId);
  }

  function setPopupOpen(menuId, buttonId, open) {
    return popupMenus
      ? popupMenus.setOpen(menuId, buttonId, open) : false;
  }

  function toggleTemplateMenu(forceClose) {
    var open = !forceClose && !popupIsOpen('orchTplMenu');
    if (open) storeBrowser.close();
    setPopupOpen('orchTplMenu', 'orchTplBtn', open);
    return open;
  }

  function loadBlankDraft() {
    if (typeof options.blankDefinition !== 'function') return null;
    var definition = options.blankDefinition();
    var adoption = _applyDefinitionResult(definition, null);
    return adoption.ok ? definition : null;
  }

  async function loadBuiltin(name, opts) {
    opts = opts || {};
    if (!authoringRequest.canLoadBuiltin()) {
      if (opts.initial) return loadBlankDraft();
      _toast(_translate('orch.api.unavailable'), true);
      return null;
    }
    var result = await authoringRequest.loadBuiltin(name);
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'builtin', result.cause);
    }
    if (!result.ok) {
      if (opts.initial) return loadBlankDraft();
      _toast(_translate('orch.builtin.loadFailed', { name: name })
        + ': ' + (result.error || _translate(
          orchestrationRequestFailureKey(result))), true);
      return null;
    }
    var adoption = _applyDefinitionResult(result.definition, null, {
      inspection: name === 'blank' ? null : (result.inspection || null),
    });
    if (!adoption.ok) {
      if (adoption.cause) {
        reportOrchestrationDiagnostic(options.onError, 'adopt', adoption.cause);
      }
      _toast(_translate('orch.builtin.loadFailed', { name: name })
        + ': ' + _translate('orch.store.readFailed'), true);
      return null;
    }
    if (!opts.initial) {
      _toast(_translate('orch.builtin.loaded', { name: name }));
    }
    return result.definition;
  }

  async function chooseBuiltin(name) {
    if (typeof options.confirmReplace === 'function'
        && !await options.confirmReplace()) return null;
    toggleTemplateMenu(true);
    return loadBuiltin(name);
  }

  async function tidy(opts) {
    opts = opts || {};
    var silent = !!opts.silent;
    if (typeof options.nodeCount === 'function' && !options.nodeCount()) {
      return null;
    }
    if (!authoringRequest.canLayout()) {
      if (!silent) _toast(_translate('orch.api.unavailable'), true);
      return null;
    }
    var definition = typeof options.currentLevelDefinition === 'function'
      ? options.currentLevelDefinition() : null;
    var lifecycle = options.lifecycle;
    var layoutRevision = lifecycle && typeof lifecycle.revision === 'function'
      ? lifecycle.revision() : null;
    var layoutWorkspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : null;
    var result = await authoringRequest.layout(definition);
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'layout', result.cause);
    }
    if (!result.ok) {
      if (!silent) {
        _toast(_translate('orch.tidy.failed')
          + ': ' + (result.error || _translate(
            orchestrationRequestFailureKey(result))), true);
      }
      return null;
    }
    var currentRevision = lifecycle && typeof lifecycle.revision === 'function'
      ? lifecycle.revision() : layoutRevision;
    var currentWorkspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : layoutWorkspace;
    if (currentRevision !== layoutRevision
        || currentWorkspace !== layoutWorkspace) {
      if (!silent) _toast(_translate('orch.tidy.stale'));
      return null;
    }
    var projection = result.positions
      ? { ok: true, positions: result.positions }
      : projectOrchestrationLayoutPositions(result.definition, definition);
    if (!projection.ok) {
      if (projection.cause) {
        reportOrchestrationDiagnostic(options.onError, 'layout', projection.cause);
      }
      if (!silent) {
        _toast(_translate('orch.tidy.failed') + ': '
          + (projection.code || _translate('orch.store.readFailed')), true);
      }
      return null;
    }
    if (typeof options.applyPositions === 'function') {
      options.applyPositions(projection.positions);
    }
    if (!opts.preserveDocumentState
        && lifecycle && typeof lifecycle.markDirty === 'function') {
      lifecycle.markDirty();
    } else if (opts.preserveDocumentState
        && lifecycle && typeof lifecycle.syncHistory === 'function') {
      lifecycle.syncHistory();
    }
    if (typeof options.render === 'function') options.render();
    if (typeof options.fitView === 'function') options.fitView();
    if (!silent) _toast(_translate('orch.tidy.done'));
    return result.definition;
  }

  return {
    toggleTemplateMenu: toggleTemplateMenu,
    loadBuiltin: loadBuiltin,
    chooseBuiltin: chooseBuiltin,
    loadBlankDraft: loadBlankDraft,
    tidy: tidy,
    save: persistence.save,
    saveAndUse: persistence.saveAndUse,
    openLoadMenu: function (forceOpen) {
      return storeBrowser.open(forceOpen);
    },
    loadFromStore: loadFromStore,
    deleteFromStore: deleteFromStore,
  };
}
