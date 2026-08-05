/* ═══════════════════════════════════════════════════════════════════
   feature-loader.js — on-demand loader for the DEFERRED feature bundle.

   The app ships as TWO bundles (see lib/js_bundler.py):
     • the CORE boot bundle (bundle-<hash>.js) — everything needed for
       first paint + chat, loaded eagerly as today;
     • the FEATURE bundle (feature-<hash>.js) — heavy, rarely-first-used
       modules (paper reader, orchestration studio, task mode) that most
       sessions never open. It is NOT loaded at boot; it is fetched the
       first time the user actually opens one of those features.

   Both bundles run in the SAME global (window) scope — they are plain
   concatenated <script>s, NOT ES modules — so once the feature bundle
   loads, its real functions (togglePaperMode / openOrchestration /
   openTaskMode …) OVERWRITE the tiny stubs installed here, and every
   later call hits the real implementation directly with zero overhead.

   The feature bundle's hashed URL is injected into the page by
   routes/common.py as `window.__FEATURE_BUNDLE_SRC__`. When bundling is
   disabled / failed (dev fallback → individual <script> tags), that
   global is absent and the real functions are already present, so the
   stubs are never installed (see _installFeatureStub guard).

   This file is CORE (in _BUNDLE_FILES) — it must load before main.js.
   ═══════════════════════════════════════════════════════════════════ */

/* Run `fn` as soon as the DOM is ready. If the document has already
 * finished parsing (the common case when a module is loaded LATE, after
 * boot, via the feature bundle) `fn` runs synchronously — a plain
 * `addEventListener('DOMContentLoaded', …)` would silently never fire in
 * that case. Deferred modules use this instead of a bare DOMContentLoaded
 * listener so their init runs identically whether they load at boot or
 * on-demand. */
function _onReady(fn) {
  try {
    if (document.readyState === 'loading')
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    else
      fn();
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[feature-loader] _onReady failed: ' + (e && e.message), 'warn');
  }
}
window._onReady = _onReady;

/* Idempotent feature-bundle loader. Returns a Promise that resolves once
 * the feature bundle <script> has loaded (or immediately if it's already
 * loaded / there is nothing to load). Concurrent callers share ONE
 * in-flight load — the <script> is injected at most once. */
let _featureBundlePromise = null;
function _loadFeatureBundle() {
  if (_featureBundlePromise) return _featureBundlePromise;
  const src = window.__FEATURE_BUNDLE_SRC__;
  if (!src) {
    /* Dev fallback (individual <script> tags) or bundling disabled: the
     * real feature functions are already on the page. Nothing to load. */
    _featureBundlePromise = Promise.resolve(false);
    return _featureBundlePromise;
  }
  _featureBundlePromise = new Promise((resolve) => {
    try {
      const s = document.createElement('script');
      s.src = src;
      s.async = false;   // preserve execution order relative to any sibling injected scripts
      s.onload = () => {
        if (typeof debugLog === 'function') debugLog('[feature-loader] feature bundle loaded', 'success');
        /* Notify wrappers that captured a STUB while the bundle was in
         * flight (mobile_panels.js's identity-tracked _wrapOne listens for
         * this to re-wrap the REAL functions — without the event the
         * wrapper would hold the dead stub forever). */
        try { document.dispatchEvent(new CustomEvent('tofu:feature-bundle-loaded')); }
        catch (e) { if (typeof debugLog === 'function') debugLog('[feature-loader] loaded-event dispatch failed: ' + (e && e.message), 'warn'); }
        resolve(true);
      };
      s.onerror = () => {
        /* Reset so a later click retries the load rather than being stuck
         * on a permanently-rejected promise (flaky network). */
        _featureBundlePromise = null;
        console.error('[feature-loader] failed to load feature bundle:', src);
        if (typeof debugLog === 'function') debugLog('[feature-loader] feature bundle load FAILED', 'error');
        resolve(false);
      };
      document.head.appendChild(s);
    } catch (e) {
      _featureBundlePromise = null;
      console.error('[feature-loader] inject error:', e);
      resolve(false);
    }
  });
  return _featureBundlePromise;
}
window._loadFeatureBundle = _loadFeatureBundle;

/* Install a lazy stub for a deferred entry-point function. The stub loads
 * the feature bundle, then dispatches to the REAL function the bundle
 * defined (which by then has overwritten this stub). Guards against the
 * degenerate case where the bundle failed to define the real fn (the stub
 * would otherwise recurse) by checking identity. Zero-arg entry points are
 * the norm (all are onclick= handlers), but args are forwarded for safety. */
function _installFeatureStub(name) {
  /* If the real function is ALREADY present, do not clobber it with a
   * stub. Two cases:
   *   1. dev fallback (individual <script> tags): no __FEATURE_BUNDLE_SRC__,
   *      the real feature functions are already on the page;
   *   2. MIXED-SHAPE transition (2026-08-01 conv-sync incident): a core
   *      bundle built from a manifest that still INLINES the module
   *      (real fn defined) served together with a feature-loader whose
   *      stub list already names it. Clobbering the real fn here kills
   *      its wiring the moment the feature bundle 404s or lacks the
   *      not-yet-deferred module. Skip — the real fn wins. */
  if (!window.__FEATURE_BUNDLE_SRC__) return;
  if (typeof window[name] === 'function') return;
  const stub = function () {
    const args = arguments;
    _loadFeatureBundle().then((loaded) => {
      // `name` is a dynamic global key; index a string-record view of window
      // (scoped cast, not a blanket Window index signature that would mask typos).
      const real = /** @type {Record<string, any>} */ (window)[name];
      if (typeof real === 'function' && real !== stub) {
        try { real.apply(null, args); }
        catch (e) { console.error('[feature-loader] ' + name + ' threw:', e); }
      } else if (!loaded) {
        if (typeof toast === 'function') toast(t('feature.loadFailed'), 'error');
      } else {
        console.error('[feature-loader] ' + name + ' not defined after feature bundle load');
      }
    });
  };
  /** @type {Record<string, any>} */ (window)[name] = stub;
}

/* The deferred entry points. Keep in sync with _DEFERRED_ENTRY_POINTS in
 * lib/js_bundler.py (orchestration / task-mode / paper-reader / image-gen).
 * All are onclick= handlers in index.html. enterImageGenMode is the real
 * load trigger for image-gen; the panel controls (selectIg…, exit, generate,
 * toggleIgModelDropdown) only become clickable after the panel opens (which
 * loads the bundle), but are stubbed for defense-in-depth. */
const _DEFERRED_ENTRY_POINTS = [
  'openOrchestration', 'openTaskMode', 'togglePaperMode',
  'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect',
  'selectIgAspect', 'selectIgCount', 'selectIgResolution', 'toggleIgModelDropdown',
  // Project Brain openers (deferred 2026-07-09). Only the user-triggered
  // openers — projectBrainRefresh (conv-switch) + closeProjectBrain (overlay
  // onclick) are intentionally NOT here so conv-switch never loads the bundle.
  'openProjectBrain', 'toggleProjectBrain', 'openProjectBrainInfluence',
  // Cross-tab sync boot wiring (deferred 2026-07-31, Epic-E sub-part 3
  // slice A). main.js calls _wireConvSyncPush typeof-guarded at boot —
  // this stub loads the feature bundle and dispatches to the real fn, so
  // the conv-sync push subscription still wires right after boot.
  '_wireConvSyncPush',
  // My Day modal (deferred 2026-08-01, Epic-E sub-6). openDailyReport is
  // the real entry (always-visible topbar button); the other two are
  // defense-in-depth (only clickable inside the open modal).
  'openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate',
  // Project panel (deferred 2026-08-01, Epic-E sub-7). openProjectModal is
  // the always-visible project-bar opener; the rest are chat-rendered
  // interactive handlers (write-approval, stdin, human-guidance, undo/redo
  // modification cards, apply-code modal) — a click while the panel is in
  // flight must load the bundle and dispatch, never ReferenceError.
  'openProjectModal', 'closeProjectModal',
  'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
  'submitHumanGuidanceChoice', 'submitHumanGuidanceFreeText',
  'undoConvModifications', 'undoAllModifications', 'redoConvModifications',
  'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
  // Cost popover (deferred 2026-08-01, Epic-E sub-8) — chat-rendered on
  // every assistant message with cost info; click loads the bundle, then
  // builds + shows the popover from the _costCtxByMsg stash.
  '_toggleCostPopover',
  // Settings-panel six-pack (deferred 2026-08-01, Epic-E sub-9).
  // Badge/tab/mobile-sheet entries + the three settings-core-panel tab
  // populates (gate+stub: the typeof gate passes on the stub, which
  // loads the bundle and dispatches). The memory-modal pair is
  // defense-in-depth (reachable only inside the open modal).
  'openUpdateDialog', 'toggleTimerPanel', 'toggleOptimizerPanel',
  'toggleMemory', 'openMemoryModal', 'closeMemoryModal',
  'toggleMemoryAddForm', 'toggleMemoryFromModal',
  '_populateSkillsTab', '_populatePreferencesTab',
  '_renderSettingsUpdatePill',
  // Defense-in-depth close-out (same slice): static panel onclicks
  // clickable in the settings-open → bundle-land window — updateModal
  // overlay closer, skills scope tabs + search, memory create form,
  // preferences reload/save (image-gen precedent: stub every control
  // reachable from server-spliced static panel HTML).
  'closeUpdateModal', '_skillsSetScope', '_skillsFilter',
  'openMemoryCreateForm', 'refreshPreferences', 'savePreferences',
  // Settings modal (deferred 2026-08-01, Epic-E sub-10) — openSettings is
  // the real entry (sidebar gear / mobile sheet / onboarding); the rest
  // are defense-in-depth (only clickable inside the open modal).
  'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
];
_DEFERRED_ENTRY_POINTS.forEach(_installFeatureStub);
window._DEFERRED_ENTRY_POINTS = _DEFERRED_ENTRY_POINTS;

/* Warm the deferred feature bundle during browser idle time after boot.
 * Without this, the FIRST click on Paper / Studio / Task-mode / Image-gen
 * blocks on the whole feature bundle's network fetch + parse before the mode
 * can open — a visible lag. Prefetching in the background turns that first
 * click instant. _loadFeatureBundle() is idempotent (one shared promise), so
 * a click that races the prefetch simply awaits the same in-flight load; and
 * when there is nothing to defer (dev fallback) it no-ops. Deferred to idle so
 * it never competes with first paint / chat boot. */
if (window.__FEATURE_BUNDLE_SRC__) {
  const _prefetchFeatureBundle = function () {
    try { _loadFeatureBundle(); }
    catch (e) {
      if (typeof debugLog === 'function') debugLog('[feature-loader] prefetch failed: ' + (e && e.message), 'warn');
    }
  };
  if (typeof requestIdleCallback === 'function')
    requestIdleCallback(_prefetchFeatureBundle, { timeout: 5000 });
  else
    setTimeout(_prefetchFeatureBundle, 2000);
}
