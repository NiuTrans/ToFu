/* ===== migrated source: ui/workunit_tracker.js ===== */
/* Explicit composition ports only. Delete with retained multimedia dispatch;
 * conversation/ui/work-unit owns rendering, requests and mounted lifecycles. */
let _workUnitCards = null;
function _wuScanSoon() {
  if (!_workUnitCards) {
    _workUnitCards = createWorkUnitTracker({
      document,
      text: (key, fallback) => t(key, fallback),
      ownerId: () => runtimeScope._currentUserId,
      getSnapshot: id => Api.tasks.get(id),
      humanResponse: (id, response) => Api.chat.humanResponse(id, response),
      abort: id => Api.tasks.abort(id),
      scenes: id => Api.motion.scenes(id),
      regenerate: (id, scene) => Api.motion.regenScene(id, scene),
      resolveUrl: path => resolveWorkUnitResourceUrl(path),
      sceneFileUrl: (id, scene) => Api.motion.sceneFileUrl(id, scene),
      openSlides: (id, page, total) => openSlidesEditor(id, page, total),
      prefill: message => _wuFillComposer(message),
      notify: (title, body) => { if (typeof showToast === 'function') showToast('', title, body, 2600); },
    });
    retainedCompositionLifecycle.add(() => _workUnitCards?.dispose());
  }
  _workUnitCards.demand();
}
function _wuFillComposer(message) {
  prefillWorkUnitComposer(document, message, () => updateSendButton());
}
runtimeScope.TofuComposerPrefill = _wuFillComposer;
