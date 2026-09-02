/* ===== migrated source: core/conv_apply_settings.js ===== */
/**
 * conv_apply_settings.js — settings-column → conversation adoption.
 *
 * This pure adapter is the one frontend boundary from the settings envelope
 * to the in-memory catalog row. Add fields here and to the backend contract
 * together.
 */

function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model || settings.effort || settings.preset)
    conv.model = settings.model || settings.preset || settings.effort;
  /* The provider that actually served this conv (stamped server-side at
   * task persist). The context gauge's limit lookup keys on
   * provider::model — without this mapping the gauge could never resolve
   * a legacy conv's window and showed "—" forever. */
  if (settings.provider_id) conv.provider_id = settings.provider_id;
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
  if (settings.planMode !== undefined)
    conv.planMode = settings.planMode === true;
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
  /* Autopilot run summaries — human-only sidecar (runId → {content,
   * translatedContent?, ts}). Not Turns; rendered as the run fold's
   * read-only report panel. Round-trips via the settings column. */
  if (settings.autopilotSummaries !== undefined)
    conv.autopilotSummaries = settings.autopilotSummaries;
  /* Persist last-Turn catalog facts for metadata-only shells. */
  if (settings.lastMsgRole) conv.lastMsgRole = settings.lastMsgRole;
  if (settings.lastMsgTimestamp) conv.lastMsgTimestamp = settings.lastMsgTimestamp;
  /* Settled-Turn facts for sidebar status before TurnStore hydration. Raw
   * facts only; _convStatusFlags classifies them. */
  if (settings.lastFinishReason !== undefined) conv.lastFinishReason = settings.lastFinishReason;
  if (settings.lastMsgError !== undefined) conv.lastMsgError = settings.lastMsgError;
  if (settings.lastMsgHasOutput !== undefined) conv.lastMsgHasOutput = settings.lastMsgHasOutput;
}
