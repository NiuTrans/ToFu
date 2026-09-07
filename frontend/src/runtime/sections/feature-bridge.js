/* ===== migrated source: feature-bridge.js ===== */
/* Required Vite feature-entry bridge.
 *
 * The core shell is still a classic bundle, so inline handlers need small
 * globals before the Vite graph has evaluated. These stubs wait for the one
 * registered owner and invoke it exactly once. There is deliberately no
 * all-feature bundle, raw-script, or module-failure rollback path.
 */

function _onReady(fn) {
  try {
    if (document.readyState === 'loading')
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  } catch (e) {
    console.error('[feature-bridge] ready callback failed:', e);
  }
}
runtimeScope._onReady = _onReady;

function _featureLoadError(name, error) {
  console.error('[feature-bridge] required owner ' + name + ' failed:', error);
  try {
    if (typeof showToast === 'function') showToast(t('feature.loadFailed'), 'error');
  } catch (_) { /* the console error remains authoritative */ }
}

/* One bounded self-heal for a module graph that never became ready: a failed
 * chunk fetch is cached by the browser module map for the document's whole
 * lifetime, so only a reload can clear it. Guard interval and storage keys
 * mirror frontend/src/core/feature-load-recovery.ts (parity pinned by
 * tests/test_frontend_feature_load_recovery.py); this classic bridge cannot
 * import that typed owner because the failed ESM graph is exactly what it
 * must survive. */
function _attemptModuleGraphRecovery(name) {
  try {
    var now = Date.now();
    var last = Number(window.sessionStorage.getItem('tofu:feature-load-reload') || 0);
    if (Number.isFinite(last) && last > 0 && now - last < 60000) return false;
    window.sessionStorage.setItem('tofu:feature-load-reload', String(now));
    window.sessionStorage.setItem('tofu:feature-load-pending', name);
    window.location.reload();
    return true;
  } catch (_) {
    return false;
  }
}

function _invokeFeatureOwner(name, args, stub) {
  var bridge = window.TofuModules;
  if (!bridge || typeof bridge.invokeFeature !== 'function') return false;
  if (typeof bridge.canInvokeFeature === 'function' && !bridge.canInvokeFeature(name)) {
    _featureLoadError(name, new Error('No registered frontend owner'));
    return true;
  }
  Promise.resolve(bridge.invokeFeature(name, args, stub)).catch(function (error) {
    _featureLoadError(name, error);
  });
  return true;
}

function _installFeatureStub(name) {
  if (typeof runtimeScope[name] === 'function') return;
  var stub = function () {
    var args = Array.prototype.slice.call(arguments);
    if (_invokeFeatureOwner(name, args, stub)) return;

    var settled = false;
    var finish = function () {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      window.removeEventListener('tofu:modules-ready', finish);
      if (!_invokeFeatureOwner(name, args, stub)) {
        if (_attemptModuleGraphRecovery(name)) return;
        _featureLoadError(name, new Error('Vite module graph did not become ready'));
      }
    };
    var timer = window.setTimeout(finish, 5000);
    window.addEventListener('tofu:modules-ready', finish, { once: true });
  };
  runtimeScope[name] = stub;
}

const _FEATURE_ENTRY_POINTS = [
  'openOrchestration', 'openTaskMode', 'togglePaperMode',
 'toggleResearchMode',
  'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect',
  'selectIgAspect', 'selectIgCount', 'selectIgModel', 'selectIgResolution',
  'toggleIgModelDropdown', '_igCancelGeneration', '_igRetryGenerationTurn',
  'openProjectBrain', 'toggleProjectBrain',
  '_wireConvSyncPush',
  'openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate',
  'openKnowledgeBase', 'closeKnowledgeBase',
  'openProjectModal', 'closeProjectModal',
  'openLocalControlModal', 'closeLocalControlModal',
  'toggleBrowserFromLocalModal', 'toggleDesktopFromLocalModal',
  'toggleDebug', 'closeDebug', 'copyDebugContent',
  'openRequestInspectorForTask', 'openToolDebugPanel',
  'openCompactionViewer',
  'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
  'submitHumanGuidanceChoice', 'submitHumanGuidanceFreeText',
  'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
  '_toggleCostPopover',
  'openUpdateDialog', 'toggleTimerPanel', 'toggleOptimizerPanel',
  'toggleMemory', 'openMemoryModal', 'closeMemoryModal',
  'toggleMemoryAddForm', 'toggleMemoryFromModal',
  'installSkillFromFileInput', '_openSkillsStoreFromMemory',
  '_populateSkillsTab', '_populatePreferencesTab', '_renderSettingsUpdatePill',
  'closeUpdateModal', '_skillsSetScope', '_skillsFilter', '_skillsInstallFromInput',
  'refreshPreferences', 'savePreferences',
  'populateToolsInventory', 'searchToolsInventory',
  'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
  '_oauthLogin',
];

_FEATURE_ENTRY_POINTS.forEach(_installFeatureStub);
runtimeScope._FEATURE_ENTRY_POINTS = Object.freeze(_FEATURE_ENTRY_POINTS.slice());
