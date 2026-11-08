/**
 * Browser model-capability taxonomy.
 *
 * Responsibility: own the mutable server-projected exclusion set used by
 * every chat-model picker and the ordered model kinds (task families) that
 * group and describe models. Entry point: `createModelCapabilityTaxonomy`.
 * Dependencies: none; composition publishes one controller to retained and
 * lazy consumers after applying the latest server configuration. The
 * authority is lib/model_info/capability_taxonomy.py; the fallbacks below are
 * pinned to it by tests/model_routing/test_capability_taxonomy_parity.py.
 */

export const CHAT_EXCLUDED_CAPS_FALLBACK = Object.freeze([
  'image_gen',
  'video_gen',
  'audio_gen',
  'embedding',
  'reranking',
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
  'video_gen',
  'embedding',
  'reranking',
  'transcription',
  'tts',
  'audio_gen',
  'audio_chat',
] as const);

/** One task family. A kind is a media kind exactly when it declares media
 *  price units: media facts (media_profile/media_pricing) describe it rather
 *  than a token context window. */
export interface ModelKindDescriptor {
  readonly id: string;
  readonly capabilities: readonly string[];
  readonly output_modality: string;
  readonly endpoint: string;
  readonly media_price_units: readonly string[];
}

function kind(
  id: string,
  capabilities: readonly string[],
  outputModality: string,
  endpoint: string,
  mediaPriceUnits: readonly string[],
): ModelKindDescriptor {
  return Object.freeze({
    id,
    capabilities: Object.freeze([...capabilities]),
    output_modality: outputModality,
    endpoint,
    media_price_units: Object.freeze([...mediaPriceUnits]),
  });
}

/** Ordered kinds mirroring the backend MODEL_KINDS (order = precedence). */
export const MODEL_KINDS_FALLBACK: readonly ModelKindDescriptor[] = Object.freeze([
  kind('chat', [], 'text', 'chat_completions', []),
  kind('image_generation', ['image_gen'], 'image', 'images_generations', ['per_image']),
  kind('video_generation', ['video_gen'], 'video', 'video_generations', ['per_second', 'per_request']),
  kind('audio_generation', ['audio_gen'], 'audio', 'audio_generations', ['per_second', 'per_request']),
  kind('speech_synthesis', ['tts'], 'audio', 'audio_speech', ['per_million_characters', 'per_second']),
  kind('speech_recognition', ['transcription'], 'text', 'audio_transcriptions', ['per_minute']),
  kind('embedding', ['embedding'], 'vector', 'embeddings', []),
  kind('reranking', ['reranking'], 'score', 'rerank', []),
]);

export interface CapabilityBearingModel {
  capabilities?: readonly unknown[] | null;
}

export interface ModelCapabilityTaxonomy {
  isChatModel(model: unknown): boolean;
  applyCapabilityTaxonomy(payload: unknown): boolean;
  getChatExcludedCaps(): Set<string>;
  getKnownCapabilities(): string[];
  getModelKinds(): ModelKindDescriptor[];
  /** Kind id of a capability list: first non-default kind whose task
   *  capability is present, else the default (capability-less) kind. */
  modelKind(capabilities: unknown): string;
  isMediaKind(kindId: string): boolean;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function validCapabilityList(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every(isNonEmptyString);
}

function validModelKinds(value: unknown): value is ModelKindDescriptor[] {
  if (!Array.isArray(value) || value.length === 0) return false;
  const ids = new Set<string>();
  let defaults = 0;
  for (const candidate of value) {
    if (!candidate || typeof candidate !== 'object') return false;
    const row = candidate as Record<string, unknown>;
    if (!isNonEmptyString(row.id) || ids.has(row.id)) return false;
    if (!Array.isArray(row.capabilities) || !row.capabilities.every(isNonEmptyString)) return false;
    if (!isNonEmptyString(row.output_modality) || !isNonEmptyString(row.endpoint)) return false;
    if (!Array.isArray(row.media_price_units)
      || !row.media_price_units.every(isNonEmptyString)) return false;
    if (row.capabilities.length === 0) defaults += 1;
    ids.add(row.id);
  }
  return defaults === 1;
}

function freezeKinds(rows: readonly ModelKindDescriptor[]): readonly ModelKindDescriptor[] {
  return Object.freeze(rows.map((row) => kind(
    row.id, row.capabilities, row.output_modality, row.endpoint, row.media_price_units)));
}

/** Create an isolated taxonomy projection with the lean compiled fallback. */
export function createModelCapabilityTaxonomy(): ModelCapabilityTaxonomy {
  let chatExcludedCaps = new Set<string>(CHAT_EXCLUDED_CAPS_FALLBACK);
  let knownCapabilities: readonly string[] = KNOWN_CAPABILITIES_FALLBACK;
  let modelKinds: readonly ModelKindDescriptor[] = MODEL_KINDS_FALLBACK;

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
    // Optional: older servers omit them. Validate BEFORE committing anything
    // so a malformed field never leaves a half-applied projection.
    const knownCandidate = candidate.known_capabilities;
    if (knownCandidate !== undefined && !validCapabilityList(knownCandidate)) {
      return false;
    }
    const kindsCandidate = candidate.model_kinds;
    if (kindsCandidate !== undefined && !validModelKinds(kindsCandidate)) {
      return false;
    }
    chatExcludedCaps = new Set(candidate.chat_excluded_caps);
    if (validCapabilityList(knownCandidate)) {
      knownCapabilities = Object.freeze([...knownCandidate]);
    }
    if (validModelKinds(kindsCandidate)) {
      modelKinds = freezeKinds(kindsCandidate);
    }
    return true;
  };

  const getChatExcludedCaps = (): Set<string> => new Set(chatExcludedCaps);

  const getKnownCapabilities = (): string[] => [...knownCapabilities];

  const getModelKinds = (): ModelKindDescriptor[] => [...modelKinds];

  const modelKind = (capabilities: unknown): string => {
    const present = new Set(Array.isArray(capabilities)
      ? capabilities.filter(isNonEmptyString) : []);
    let fallback = '';
    for (const row of modelKinds) {
      if (!row.capabilities.length) {
        fallback ||= row.id;
        continue;
      }
      if (row.capabilities.some((capability) => present.has(capability))) return row.id;
    }
    return fallback;
  };

  const isMediaKind = (kindId: string): boolean => modelKinds.some((row) => (
    row.id === kindId && row.media_price_units.length > 0
  ));

  return Object.freeze({
    isChatModel,
    applyCapabilityTaxonomy,
    getChatExcludedCaps,
    getKnownCapabilities,
    getModelKinds,
    modelKind,
    isMediaKind,
  });
}
