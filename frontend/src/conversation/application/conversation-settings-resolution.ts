/**
 * Conversation settings request orchestration.
 *
 * Responsibility: project stored settings into resolver inputs, unwrap API
 * envelopes, fuse send-time config/settings resolution, and probe the fused
 * settings-write endpoint once across rolling upgrades. Entry points:
 * `buildConversationSettingsSnapshot` and
 * `createConversationSettingsResolution`. Dependencies: injected HTTP ports;
 * no DOM, transcript state, owner identity, or copy of server merge policy.
 */

import { captureConversationProject, type ProjectConversation } from './conversation-project';

type UnknownRecord = Record<string, unknown>;

export interface ConversationSettingsSource extends UnknownRecord {
  readonly model?: unknown;
  readonly modelRef?: unknown;
  readonly preferredProviderId?: unknown;
  readonly thinkingDepth?: unknown;
  readonly searchMode?: unknown;
  readonly fetchEnabled?: unknown;
  readonly codeExecEnabled?: unknown;
  readonly browserEnabled?: unknown;
  readonly desktopEnabled?: unknown;
  readonly memoryEnabled?: unknown;
  readonly schedulerEnabled?: unknown;
  readonly autopilotEnabled?: unknown;
  readonly activeFlow?: unknown;
  readonly imageGenEnabled?: unknown;
  readonly imageGenMode?: unknown;
  readonly imageGenModel?: unknown;
  readonly imageGenProviderId?: unknown;
  readonly imageGenCount?: unknown;
  readonly imageGenAspect?: unknown;
  readonly imageGenResolution?: unknown;
  readonly humanGuidanceEnabled?: unknown;
  readonly chatMode?: unknown;
  readonly planMode?: unknown;
  readonly projectPaths?: unknown;
  readonly readOnlyPaths?: unknown;
  readonly autoTranslate?: unknown;
  readonly folderId?: unknown;
  readonly autoApply?: unknown;
}

export interface ConversationSettingsInputs extends UnknownRecord {
  readonly conv_settings: UnknownRecord;
  readonly overrides: UnknownRecord;
}

export interface ConversationSettingsResponse {
  readonly ok: boolean;
  readonly status: number;
}

export interface ConversationSettingsResolutionPorts {
  resolveConfig(inputs: UnknownRecord): Promise<unknown>;
  resolveSettings(inputs: ConversationSettingsInputs): Promise<unknown>;
  patchResolvedSettings(
    conversationId: string,
    inputs: ConversationSettingsInputs,
  ): Promise<ConversationSettingsResponse>;
  patchSettings(
    conversationId: string,
    settings: UnknownRecord,
  ): Promise<ConversationSettingsResponse>;
}

export interface ConversationSubmissionResolution {
  readonly config: UnknownRecord;
  readonly settings: UnknownRecord;
}

export interface ConversationSettingsResolution {
  resolveConfig(inputs: UnknownRecord): Promise<UnknownRecord>;
  resolveSettings(inputs: ConversationSettingsInputs): Promise<UnknownRecord>;
  resolveSubmission(
    configInputs: UnknownRecord,
    settingsInputs: ConversationSettingsInputs,
  ): Promise<ConversationSubmissionResolution>;
  persist(
    conversationId: string,
    inputs: ConversationSettingsInputs,
  ): Promise<ConversationSettingsResponse>;
}

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function unwrapEnvelope(value: unknown): UnknownRecord {
  const result = { ...(record(value) || {}) };
  delete result.ok;
  delete result.request_id;
  return result;
}

export function buildConversationSettingsSnapshot(
  source: ConversationSettingsSource,
  _legacyProjectPath: unknown,
  uiLanguage: unknown,
): UnknownRecord {
  return {
    model: source.model,
    modelRef: source.modelRef,
    preferredProviderId: source.preferredProviderId,
    thinkingDepth: source.thinkingDepth,
    searchMode: source.searchMode,
    fetchEnabled: source.fetchEnabled,
    codeExecEnabled: source.codeExecEnabled,
    browserEnabled: source.browserEnabled,
    desktopEnabled: source.desktopEnabled,
    memoryEnabled: source.memoryEnabled,
    schedulerEnabled: source.schedulerEnabled,
    autopilotEnabled: source.autopilotEnabled,
    activeFlow: source.activeFlow || '',
    imageGenEnabled: source.imageGenEnabled,
    imageGenMode: source.imageGenMode,
    imageGenModel: source.imageGenModel,
    imageGenProviderId: source.imageGenProviderId,
    imageGenCount: source.imageGenCount,
    imageGenAspect: source.imageGenAspect,
    imageGenResolution: source.imageGenResolution,
    humanGuidanceEnabled: source.humanGuidanceEnabled,
    chatMode: 'chat',
    planMode: Boolean(source.planMode),
    ...captureConversationProject(source as ProjectConversation),
    autoTranslate: source.autoTranslate,
    uiLang: source.uiLang || uiLanguage,
    folderId: source.folderId,
    autoApply: source.autoApply,
  };
}

function captureSubmissionBinding(source: UnknownRecord | null | undefined): UnknownRecord {
  return {
    ...captureConversationProject(source as ProjectConversation),
    ...(source && Object.prototype.hasOwnProperty.call(source, 'folderId')
      ? { folderId: source.folderId } : {}),
  };
}

export function createConversationSettingsResolution(
  ports: ConversationSettingsResolutionPorts,
): ConversationSettingsResolution {
  let resolvedWriteSupported = true;

  const resolveConfig = async (
    inputs: UnknownRecord,
  ): Promise<UnknownRecord> => {
    const source = record(inputs.conv_settings);
    const workspace = source && captureSubmissionBinding(source);
    const resolved = unwrapEnvelope(await ports.resolveConfig(inputs));
    return workspace ? { ...resolved, ...workspace, chatMode: 'chat' } : resolved;
  };

  const resolveSettings = async (
    inputs: ConversationSettingsInputs,
  ): Promise<UnknownRecord> => {
    const workspace = captureSubmissionBinding(inputs.conv_settings);
    const resolved = unwrapEnvelope(await ports.resolveSettings(inputs));
    return { ...resolved, ...workspace, chatMode: 'chat' };
  };

  const resolveSubmission = async (
    configInputs: UnknownRecord,
    settingsInputs: ConversationSettingsInputs,
  ): Promise<ConversationSubmissionResolution> => {
    const workspace = captureSubmissionBinding(configInputs.conv_settings as UnknownRecord);
    const config = await resolveConfig({
      ...configInputs,
      settings_conv_settings: settingsInputs.conv_settings,
      include_settings: true,
    });
    const fusedSettings = record(config.settings);
    delete config.settings;
    // Resolver defaults must never fill explicit empty roots from ambient state.
    return {
      config: { ...config, ...workspace, chatMode: 'chat' },
      settings: { ...(fusedSettings || await resolveSettings(settingsInputs)), ...workspace, chatMode: 'chat' },
    };
  };

  const persist = async (
    conversationId: string,
    inputs: ConversationSettingsInputs,
  ): Promise<ConversationSettingsResponse> => {
    if (resolvedWriteSupported) {
      const response = await ports.patchResolvedSettings(
        conversationId, inputs,
      );
      if (response.ok || response.status !== 404) return response;
      resolvedWriteSupported = false;
    }
    return ports.patchSettings(
      conversationId,
      await resolveSettings(inputs),
    );
  };

  return Object.freeze({
    resolveConfig,
    resolveSettings,
    resolveSubmission,
    persist,
  });
}
