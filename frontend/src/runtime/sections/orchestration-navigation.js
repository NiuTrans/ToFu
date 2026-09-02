/* ===== migrated source: orchestration-navigation.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-navigation.js — nested Group workspace navigation

   Owns enter/exit/root-collapse transitions. Graph state remains in
   orchestration.js and is exchanged through explicit snapshots and mutation
   callbacks. The injected breadcrumb view owns hierarchy DOM and focus.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNavigationController(options) {
  options = options || {};

  function _workspace() {
    return typeof options.workspace === 'function'
      ? options.workspace() : {
        name: 'Untitled Flow', nodes: [], edges: [], selected: null, sequence: 0,
      };
  }
  function _stack() {
    var value = typeof options.stack === 'function' ? options.stack() : [];
    return Array.isArray(value) ? value : [];
  }
  function _setStack(value) {
    if (typeof options.setStack === 'function') options.setStack(value);
  }
  function _adopt(workspace) {
    if (typeof options.adopt === 'function') options.adopt(workspace);
  }
  function _render() {
    if (typeof options.render === 'function') options.render();
  }
  function _notifyNavigate() {
    if (typeof options.onNavigate === 'function') options.onNavigate();
  }
  function _fallbackName() {
    return typeof options.fallbackName === 'function'
      ? options.fallbackName() : 'Group';
  }
  function _maybeLayout(workspace) {
    if (!workspace || !workspace.needsLayout || !workspace.nodes.length) return;
    if (typeof options.tidy === 'function') options.tidy({ silent: true });
  }

  function _fallbackDefinition() {
    return typeof options.blankGroupDefinition === 'function'
      ? options.blankGroupDefinition() : null;
  }

  function _focus(workspace) {
    if (options.breadcrumb
        && typeof options.breadcrumb.focusAfterNavigation === 'function') {
      options.breadcrumb.focusAfterNavigation(workspace);
    }
  }

  function _commit(transition, shouldLayout) {
    _setStack(transition.stack);
    _adopt(transition.workspace);
    _render();
    _notifyNavigate();
    if (shouldLayout) _maybeLayout(transition.workspace);
    _focus(transition.workspace);
    return transition.workspace;
  }

  function _collapse(workspace, stack, target) {
    var changed = false;
    while (stack.length > target) {
      var transition = options.graph.exitGroup(workspace, stack);
      if (!transition) break;
      workspace = transition.workspace;
      stack = transition.stack;
      changed = true;
    }
    return { workspace: workspace, stack: stack, changed: changed };
  }

  function workspaceState() { return _workspace(); }

  function loadWorkingFromDefinition(definition) {
    var result = options.graph.workspaceFromDefinitionResult(
      definition, _fallbackName());
    if (!result.ok) return null;
    var workspace = result.workspace;
    _adopt(workspace);
    _render();
    _maybeLayout(workspace);
    return workspace;
  }

  function enterGroup(groupId) {
    var transition = options.graph.enterGroup(
      _workspace(), _stack(), groupId, _fallbackDefinition(), _fallbackName()
    );
    if (!transition) return null;
    return _commit(transition, true);
  }

  function exitGroup() {
    var transition = options.graph.exitGroup(_workspace(), _stack());
    if (!transition) return null;
    return _commit(transition, false);
  }

  function crumbTo(depth) {
    var target = Number(depth);
    target = Number.isFinite(target) ? Math.max(0, Math.floor(target)) : 0;
    var transition = _collapse(_workspace(), _stack(), target);
    if (transition.changed) _commit(transition, false);
    return transition.stack.length;
  }

  function flushToRoot() { return crumbTo(0); }

  function navigateToGroups(groupIds) {
    groupIds = Array.isArray(groupIds) ? groupIds : [];
    var transition = _collapse(_workspace(), _stack(), 0);
    var valid = true;
    for (var index = 0; index < groupIds.length; index++) {
      var entered = options.graph.enterGroup(
        transition.workspace, transition.stack, groupIds[index],
        _fallbackDefinition(), _fallbackName()
      );
      if (!entered) { valid = false; break; }
      transition.workspace = entered.workspace;
      transition.stack = entered.stack;
      transition.changed = true;
    }
    if (transition.changed) _commit(transition, true);
    return valid;
  }

  function renderBreadcrumb() {
    if (!options.breadcrumb || typeof options.breadcrumb.render !== 'function') return;
    return options.breadcrumb.render(_stack(), crumbTo);
  }

  return {
    workspaceState: workspaceState,
    adoptWorkspace: _adopt,
    loadWorkingFromDefinition: loadWorkingFromDefinition,
    enterGroup: enterGroup,
    exitGroup: exitGroup,
    flushToRoot: flushToRoot,
    navigateToGroups: navigateToGroups,
    crumbTo: crumbTo,
    renderBreadcrumb: renderBreadcrumb,
  };
}

