/* ===== migrated source: orchestration-command-bridge.js ===== */
/* Orchestration Studio command bridge.
 *
 * This file intentionally contains only the stable global commands used by
 * the shell, inline-compatible extensions and focused controller callbacks.
 * Controller construction remains in orchestration.js; command behavior
 * stays owned by the injected document/run/workspace/composer ports.
 *
 * Load before orchestration.js: controller composition passes a small number
 * of these functions directly while all referenced controller variables are
 * resolved only when a command is invoked.
 */

function _orchOnRename(v) {
  _orchEditorState.setName(v || 'Untitled Flow');
  _orchMarkDirty('flow-name');
}

// Run Drawer commands — implementation lives in orchestration-run.js.
function _orchStartSeed(definition) {
  return _orchRunOverlay.startSeed(definition);
}

function _orchResetNodeRunStatus() {
  return _orchRunOverlay.reset();
}

function _orchHandleRunStateChange(state, change) {
  return _orchRunOverlay.applyChange(state, change);
}

async function _orchLoadBuiltin(name) {
  return _orchWorkspaceController.loadBuiltin(name, arguments[1] || {});
}

async function _orchChooseBuiltin(name) {
  return _orchWorkspaceController.chooseBuiltin(name);
}

async function _orchTidy(opts) {
  return _orchWorkspaceController.tidy(opts);
}

function _orchToDefinition() {
  return _orchDefinitionSnapshot.currentLevel();
}

function _orchRootDefinitionSnapshot() {
  return _orchDefinitionSnapshot.root();
}

function _orchExport() {
  return _orchExporter.exportCurrent();
}

function _orchToast(text, isErr, opts) {
  return _orchStudioApi.toast(text, isErr, opts);
}

