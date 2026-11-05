/* ===== migrated source: core/conv_apply_settings.js ===== */
/**
 * conv_apply_settings.js — settings-column → conversation adoption.
 *
 * This pure adapter is the one frontend boundary from the settings envelope
 * to the in-memory catalog row. contracts/conversation_settings_v1.json
 * declares every key read here; change the contract first.
 *
 * `projection` names the source: 'full' snapshot settings are authoritative
 * for every key, while 'partial' sources (catalog rows, local command echoes)
 * never clear state they do not carry.
 */

function _applySettingsToConv(conv, settings, projection = 'partial') {
  if (!settings) return;

  settings = protectHydratingProjectSettings(conv, settings);
  if (settings.model || settings.effort || settings.preset)
    conv.model = settings.model || settings.preset || settings.effort;
  if (settings.modelRef !== undefined)
    conv.modelRef = settings.modelRef && typeof settings.modelRef === 'object'
      ? Object.assign({}, settings.modelRef) : null;
  if (settings.preferredProviderId !== undefined)
    conv.preferredProviderId = settings.preferredProviderId || '';
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
  if (settings.delegateMode !== undefined)
    conv.delegateMode = settings.delegateMode === true;
  /* Per-conversation write approval: only an explicit false means Manual. */
  if (settings.autoApply !== undefined)
    conv.autoApply = settings.autoApply !== false;
  if (settings.imageGenModel)
    conv.imageGenModel = settings.imageGenModel;
  if (settings.imageGenProviderId !== undefined)
    conv.imageGenProviderId = settings.imageGenProviderId || '';
  if (settings.imageGenCount !== undefined)
    conv.imageGenCount = settings.imageGenCount;
  if (settings.imageGenAspect)
    conv.imageGenAspect = settings.imageGenAspect;
  if (settings.imageGenResolution)
    conv.imageGenResolution = settings.imageGenResolution;
  if (settings.projectPath !== undefined)
    conv.projectPath = settings.projectPath;

  // Passive creation provenance is navigation evidence, never role authority.
  if (settings._coordinatorSource !== undefined)
    conv._coordinatorSource = typeof settings._coordinatorSource === 'string'
      && settings._coordinatorSource.length <= 256 ? settings._coordinatorSource : null;
  if (settings.projectPaths !== undefined)
    conv.projectPaths = settings.projectPaths;
  if (projection === 'full' || settings.paperDocument !== undefined)
    conv.paperDocument = settings.paperDocument || null;
  if (settings.readOnlyPaths !== undefined)
    conv.readOnlyPaths = settings.readOnlyPaths;
  if (settings.autoTranslate !== undefined)
    conv.autoTranslate = settings.autoTranslate;
  if (settings.pinned !== undefined) conv.pinned = settings.pinned;
  if (settings.pinnedAt !== undefined) conv.pinnedAt = settings.pinnedAt;
  if (settings.folderId !== undefined) conv.folderId = settings.folderId;
  if (settings.source) conv.source = settings.source;
  if (settings.clonedFrom) conv.clonedFrom = settings.clonedFrom;
  /* Autopilot run summaries — human-only sidecar (runId → {content,
   * translatedContent?, ts}). Not Turns; rendered as the run fold's
   * read-only report panel. Round-trips via the settings column. */
  if (settings.autopilotSummaries !== undefined)
    conv.autopilotSummaries = settings.autopilotSummaries;
}
