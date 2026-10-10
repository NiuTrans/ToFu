/**
 * Browser model-capability taxonomy.
 *
 * Responsibility: own the mutable server-projected exclusion set used by
 * every chat-model picker. Entry point: `createModelCapabilityTaxonomy`.
 * Dependencies: none; composition publishes one controller to retained and
 * lazy consumers after applying the latest server configuration.
 */

export const CHAT_EXCLUDED_CAPS_FALLBACK = Object.freeze([
  'image_gen',
  'embedding',
  'transcription',
  'tts',
] as const);

/**
 * Ordered capability-toggle list, mirroring the backend taxonomy's
 * KNOWN_CAPABILITIES. The settings toggle grids render this until the
 * server's ``known_capabilities`` arrives; the parity test pins it to
 * lib/model_info/capability_taxonomy.py so the two never drift.
 */
export const KNOWN_CAPABILITIES_FALLBACK = Object.freeze([
  'text',
  'vision',
  'video',
  'thinking',
  'cheap',
  'image_gen',
  'embedding',
  'transcription',
  'tts',
  'audio_chat',
] as const);

export interface CapabilityBearingModel {
  capabilities?: readonly unknown[] | null;
}

export interface ModelCapabilityTaxonomy {
  isChatModel(model: unknown): boolean;
  applyCapabilityTaxonomy(payload: unknown): boolean;
  getChatExcludedCaps(): Set<string>;
  getKnownCapabilities(): string[];
}

function validCapabilityList(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every((capability) => (
      typeof capability === 'string' && capability.length > 0
    ));
}

/** Create an isolated taxonomy projection with the lean compiled fallback. */
export function createModelCapabilityTaxonomy(): ModelCapabilityTaxonomy {
  let chatExcludedCaps = new Set<string>(CHAT_EXCLUDED_CAPS_FALLBACK);
  let knownCapabilities: readonly string[] = KNOWN_CAPABILITIES_FALLBACK;

  const isChatModel = (value: unknown): boolean => {
    if (!value || typeof value !== 'object') return true;
    const model = value as CapabilityBearingModel;
    const capabilities = Array.isArray(model.capabilities)
      ? model.capabilities : [];
    return !capabilities.some((capability) => (
      typeof capability === 'string' && chatExcludedCaps.has(capability)
    ));
  };

  const applyCapabilityTaxonomy = (payload: unknown): boolean => {
    if (!payload || typeof payload !== 'object') return false;
    const candidate = payload as Record<string, unknown>;
    if (!validCapabilityList(candidate.chat_excluded_caps)) return false;
    // Optional: older servers omit it. Validate BEFORE committing anything
    // so a malformed field never leaves a half-applied projection.
    const knownCandidate = candidate.known_capabilities;
    if (knownCandidate !== undefined && !validCapabilityList(knownCandidate)) {
      return false;
    }
    chatExcludedCaps = new Set(candidate.chat_excluded_caps);
    if (validCapabilityList(knownCandidate)) {
      knownCapabilities = Object.freeze([...knownCandidate]);
    }
    return true;
  };

  const getChatExcludedCaps = (): Set<string> => new Set(chatExcludedCaps);

  const getKnownCapabilities = (): string[] => [...knownCapabilities];

  return Object.freeze({
    isChatModel,
    applyCapabilityTaxonomy,
    getChatExcludedCaps,
    getKnownCapabilities,
  });
}
