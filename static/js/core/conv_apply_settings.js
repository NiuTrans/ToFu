/**
 * conv_apply_settings.js — settings-column → conversation adoption.
 *
 * Extracted from static/js/core/conversations.js as pt_3879f00e sub-part 2
 * slice 5. `_applySettingsToConv(conv, settings)` is called from 9 sites
 * across the bundle:
 *   - 8 sites inside conversations.js (mergeServerConvShells,
 *     hydrateSidebarFromCache, _openConvMayHoldOrphanGhost,
 *     loadConversationsFromServer × 3, loadConversationMessages,
 *     forceRecoverFromServer)
 *   - 1 site inside cross_tab_sync.js (_handleConvNotifyPush) — this is
 *     the load-order-critical caller: cross_tab_sync sits at bundle
 *     index ~451, before conversations.js at ~492. The leaf MUST be
 *     inserted BEFORE cross_tab_sync in _BUNDLE_FILES so the bare name
 *     resolves at runtime via the shared window scope the bundler creates
 *     by concatenating files without a module wrapper.
 *
 * Bundle scope note: The bundler concatenates files at file scope, so a
 * top-level `function` declaration here IS available under its bare name
 * to every subsequent file in the manifest. No `window._applySettingsToConv
 * = ...` line is needed (and adding one would only slightly harden the
 * contract at zero cost — kept off deliberately to preserve byte-identity
 * with the pre-slice inline form).
 *
 * The function is a pure "reader" of `settings` and "writer" onto `conv`
 * — no module state, no closures, no async, no side effects outside the
 * two arguments. Every documented field on the settings envelope has an
 * `undefined`-guarded assignment; new fields must be added here (and to
 * the backend serialisation) simultaneously so shells hydrated from the
 * server-meta path stay in sync with those built from the /messages GET.
 */

function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model || settings.effort || settings.preset)
    conv.model = settings.model || settings.preset || settings.effort;
  if (settings.thinkingDepth) conv.thinkingDepth = settings.thinkingDepth;
  if (settings.searchMode) conv.searchMode = settings.searchMode;
  if (settings.fetchEnabled !== undefined)
    conv.fetchEnabled = settings.fetchEnabled;
  if (settings.codeExecEnabled !== undefined)
    conv.codeExecEnabled = settings.codeExecEnabled;
  if (settings.browserEnabled !== undefined)
    conv.browserEnabled = settings.browserEnabled;
  if (settings.desktopEnabled !== undefined)
    conv.desktopEnabled = settings.desktopEnabled;
  if (settings.memoryEnabled !== undefined)
    conv.memoryEnabled = settings.memoryEnabled;
  if (settings.schedulerEnabled !== undefined)
    conv.schedulerEnabled = settings.schedulerEnabled;
  if (settings.swarmEnabled !== undefined)
    conv.swarmEnabled = settings.swarmEnabled;
  if (settings.endpointEnabled !== undefined)
    conv.endpointEnabled = settings.endpointEnabled;
  if (settings.autopilotEnabled !== undefined)
    conv.autopilotEnabled = settings.autopilotEnabled;
  if (settings.activeFlow !== undefined)
    conv.activeFlow = settings.activeFlow;
  if (settings.imageGenEnabled !== undefined)
    conv.imageGenEnabled = settings.imageGenEnabled;
  if (settings.imageGenMode !== undefined)
    conv.imageGenMode = settings.imageGenMode;
  if (settings.humanGuidanceEnabled !== undefined)
    conv.humanGuidanceEnabled = settings.humanGuidanceEnabled;
  if (settings.imageGenModel)
    conv.imageGenModel = settings.imageGenModel;
  if (settings.projectSummary !== undefined)
    conv.projectSummary = settings.projectSummary;
  if (settings.projectPath !== undefined)
    conv.projectPath = settings.projectPath;
  if (settings.projectPaths !== undefined)
    conv.projectPaths = settings.projectPaths;
  if (settings.readOnlyPaths !== undefined)
    conv.readOnlyPaths = settings.readOnlyPaths;
  if (settings.autoTranslate !== undefined)
    conv.autoTranslate = settings.autoTranslate;
  if (settings.pinned !== undefined) conv.pinned = settings.pinned;
  if (settings.pinnedAt !== undefined) conv.pinnedAt = settings.pinnedAt;
  if (settings.folderId !== undefined) conv.folderId = settings.folderId;
  if (settings.source) conv.source = settings.source;
  if (settings.feishuUser) conv.feishuUser = settings.feishuUser;
  /* ★ Autopilot run summaries — human-only sidecar (runId → {content,
   * translatedContent?, ts}). NOT chat messages; rendered as the run fold's
   * read-only report panel. Round-trips via the settings column. */
  if (settings.autopilotSummaries !== undefined)
    conv.autopilotSummaries = settings.autopilotSummaries;
  /* ★ Persist last message info for Case E orphan detection on _needsLoad shells */
  if (settings.lastMsgRole) conv.lastMsgRole = settings.lastMsgRole;
  if (settings.lastMsgTimestamp) conv.lastMsgTimestamp = settings.lastMsgTimestamp;
  /* ★ Settled-turn facts for the sidebar incomplete/errored dot on a
   * messages-stripped (?meta=1) shell. Raw facts only; _convStatusFlags
   * classifies. undefined-guarded so a shell without them just falls back to
   * the messages path once loaded. */
  if (settings.lastFinishReason !== undefined) conv.lastFinishReason = settings.lastFinishReason;
  if (settings.lastMsgError !== undefined) conv.lastMsgError = settings.lastMsgError;
  if (settings.lastMsgHasOutput !== undefined) conv.lastMsgHasOutput = settings.lastMsgHasOutput;
  /* ★ Phase 3: server-authoritative ghost reconcile marker. When the backend's
   *   recover_stale_tasks_on_startup swept buried ghosts / classified the tail,
   *   it stamps settings._reconciledAt. The frontend Case-D defers to it (skips
   *   its own content-length classification) so lifecycle state is inferred in
   *   ONE place — the backend. */
  if (settings._reconciledAt) conv._reconciledAt = settings._reconciledAt;
  /* ★ Restore activeTaskId from server settings — enables Case B recovery
   *   even on a fresh browser session (no localStorage).
   *   Guard: if activeTaskId was cleared locally during this session
   *   (_activeTaskClearedAt exists), NEVER restore from server metadata.
   *   This prevents stale activeTaskId values stuck in DB from causing
   *   phantom purple dots on the sidebar.  On a true page refresh,
   *   _activeTaskClearedAt won't exist (ephemeral), so initActiveTasks
   *   will properly validate against /api/chat/active before restoring. */
  if (settings.activeTaskId && !conv.activeTaskId) {
    if (!conv._activeTaskClearedAt) {
      conv.activeTaskId = settings.activeTaskId;
    }
  }
}
