/* ===== migrated source: orchestration-lifecycle-bridge.js ===== */
/* Orchestration Studio lifecycle/document/history bridge.
 *
 * Stable globals used by the feature loader, shell and compatible extensions.
 * Controller construction stays in orchestration.js; this bridge only forwards
 * commands to the composed Studio API, document and history ports. Load before
 * orchestration.js so every referenced controller is available when invoked.
 */

function openOrchestration() {
  /* Studio is an unfinished product surface. Hiding its launch controls is
   * not enough: compatibility callers and cached action markup must fail
   * closed under the same deployment flag until the product is released. */
  if (typeof _featureFlags === 'undefined'
      || _featureFlags.debug_mode !== true) return false;
  return _orchStudioApi.open();
}

async function closeOrchestration(evt, force) {
  return _orchStudioApi.close(evt, force);
}

function _orchRenderDocState() {
  _orchDocument.render();
}

function _orchMarkDirty(historyGroup) {
  return _orchEditLifecycle.markDirty(historyGroup);
}

function _orchRenderHistoryState(state) {
  state = state || _orchEditLifecycle.historyState();
  var undo = document.getElementById('orchUndoBtn');
  var redo = document.getElementById('orchRedoBtn');
  if (undo) undo.disabled = !state.canUndo;
  if (redo) redo.disabled = !state.canRedo;
}

function _orchUndo() {
  return _orchEditLifecycle.undo();
}

function _orchRedo() {
  return _orchEditLifecycle.redo();
}

function _orchConfirmReplace() {
  return _orchDocument.confirmReplace();
}
