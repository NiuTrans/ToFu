/**
 * Model-kind and media-fact presentation for the Settings model catalog.
 *
 * Responsibility: turn taxonomy ids (kinds, capability tags, modalities,
 * media price units) and the v2 media facts into localized, escaped-at-use
 * text and inline glyphs. Pure and DOM-free; classification itself belongs
 * to frontend/src/core/model-capability-taxonomy.ts (server-projected) and
 * prices are formatted by the caller's localized price formatter.
 */

import {
  KNOWN_CAPABILITIES_FALLBACK,
  MODEL_KINDS_FALLBACK,
} from '../../core/model-capability-taxonomy';
import { t, type I18nKey } from '../../i18n';
import type {
  ModelRoutingMediaPriceRate,
  ModelRoutingMediaPricing,
  ModelRoutingMediaProfile,
} from '../../api/api-v1-model-routing.generated';

const LABELLED_KINDS: ReadonlySet<string> = new Set(MODEL_KINDS_FALLBACK.map((kind) => kind.id));
const LABELLED_CAPABILITIES: ReadonlySet<string> = new Set(KNOWN_CAPABILITIES_FALLBACK);
const LABELLED_MODALITIES: ReadonlySet<string> = new Set(['text', 'image', 'video', 'audio']);
const LABELLED_UNITS: ReadonlySet<string> = new Set([
  'per_image', 'per_second', 'per_request', 'per_minute', 'per_million_characters',
]);

/** Parameterless copy addressed by a computed key from a closed id family. */
function label(key: string): string {
  return t(key as I18nKey, {} as never);
}

/** Localized kind name; a kind this client has no copy for keeps its id. */
export function kindLabel(kindId: string): string {
  return LABELLED_KINDS.has(kindId) ? label(`modelKind.${kindId}`) : kindId;
}

export function capabilityLabel(capability: string): string {
  return LABELLED_CAPABILITIES.has(capability) ? label(`modelCapability.${capability}`) : capability;
}

export function modalityLabel(modality: string): string {
  return LABELLED_MODALITIES.has(modality) ? label(`modelModality.${modality}`) : modality;
}

const GLYPH_PATHS: Readonly<Record<string, string>> = {
  chat: '<path d="M5 6h14v9H9l-4 3z"/>',
  image_generation: '<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M5 17l5-5 4 4 2-2 3 3"/>',
  video_generation: '<rect x="3.5" y="6" width="12" height="12" rx="2"/><path d="M15.5 10.5l5-3v9l-5-3z"/>',
  audio_generation: '<path d="M9 17V6l10-2v11"/><circle cx="7" cy="17" r="2"/><circle cx="17" cy="15" r="2"/>',
  speech_synthesis: '<path d="M4 10v4h4l5 4V6L8 10z"/><path d="M16 9a4 4 0 010 6M18.5 7a7 7 0 010 10"/>',
  speech_recognition: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0012 0M12 17v3.5"/>',
  embedding: '<circle cx="6" cy="7" r="1.6"/><circle cx="17" cy="6" r="1.6"/><circle cx="12" cy="13" r="1.6"/><circle cx="7" cy="18" r="1.6"/><circle cx="18" cy="17" r="1.6"/>',
  reranking: '<path d="M7 5v14M4 16l3 3 3-3M14 7h6M14 12h4M14 17h2"/>',
};

/** Inline 16px stroke glyph for a kind (generic dot for unknown kinds). */
export function kindGlyph(kindId: string): string {
  const paths = GLYPH_PATHS[kindId] ?? '<circle cx="12" cy="12" r="3"/>';
  return '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + paths + '</svg>';
}

/** One rate as `≈$0.04/image`; estimated amounts carry the ≈ prefix. */
export function formatMediaRate(
  rate: Pick<ModelRoutingMediaPriceRate, 'unit' | 'price' | 'estimated'>,
  formatPrice: (value: number) => string,
): string {
  const price = (rate.estimated ? '≈' : '') + formatPrice(rate.price);
  return LABELLED_UNITS.has(rate.unit)
    ? t(`modelPriceUnit.${rate.unit}` as I18nKey, { price } as never)
    : `${price}/${rate.unit}`;
}

/**
 * Compact price line: the lowest rate per unit (`from` when tiers differ),
 * units in first-listed order. Null when nothing is priced.
 */
export function mediaPriceSummary(
  pricing: ModelRoutingMediaPricing | null | undefined,
  formatPrice: (value: number) => string,
): string | null {
  if (!pricing?.rates?.length) return null;
  const byUnit = new Map<string, ModelRoutingMediaPriceRate[]>();
  for (const rate of pricing.rates) {
    const bucket = byUnit.get(rate.unit) ?? [];
    bucket.push(rate);
    byUnit.set(rate.unit, bucket);
  }
  const parts: string[] = [];
  for (const rates of byUnit.values()) {
    const cheapest = rates.reduce((low, rate) => (rate.price < low.price ? rate : low));
    const varies = rates.some((rate) => rate.price !== cheapest.price);
    const text = formatMediaRate(cheapest, formatPrice);
    parts.push(varies ? t('modelCatalog.media.from', { price: text }) : text);
  }
  return parts.join(' · ');
}

/** `Text + Image → Video`, or null when the profile documents no modality. */
export function modalityFlow(profile: ModelRoutingMediaProfile | null | undefined): string | null {
  const inputs = profile?.input_modalities ?? [];
  const outputs = profile?.output_modalities ?? [];
  if (!inputs.length && !outputs.length) return null;
  const side = (values: readonly string[]) => (values.length
    ? values.map(modalityLabel).join(' + ') : '—');
  return `${side(inputs)} → ${side(outputs)}`;
}

export interface MediaFactLine {
  readonly label: string;
  readonly value: string;
}

function list(values: readonly (string | number)[] | undefined): string | null {
  return values?.length ? values.join(' / ') : null;
}

function mebibytes(bytes: number): string {
  const value = bytes / (1024 * 1024);
  return `${Number(value.toFixed(value >= 10 ? 0 : 1))} MiB`;
}

/** Documented profile facts in reading order; undocumented facts are omitted. */
export function mediaFactLines(profile: ModelRoutingMediaProfile | null | undefined): MediaFactLine[] {
  if (!profile) return [];
  const lines: MediaFactLine[] = [];
  const push = (key: I18nKey, value: string | null) => {
    if (value) lines.push({ label: label(key), value });
  };
  push('modelCatalog.media.modalities', modalityFlow(profile));
  push('modelCatalog.media.sizes', list(profile.sizes));
  push('modelCatalog.media.aspectRatios', list(profile.aspect_ratios));
  push('modelCatalog.media.resolutions', list(profile.resolutions));
  const durations = list(profile.durations_s);
  push('modelCatalog.media.durations',
    durations ? t('modelCatalog.media.seconds', { list: durations }) : null);
  push('modelCatalog.media.maxOutputs',
    profile.max_outputs ? String(profile.max_outputs) : null);
  if (typeof profile.native_audio === 'boolean') {
    push('modelCatalog.media.nativeAudio', t(profile.native_audio
      ? 'modelCatalog.media.yes' : 'modelCatalog.media.no'));
  }
  if (profile.voices?.length) {
    const shown = profile.voices.slice(0, 8).join(', ');
    push('modelCatalog.media.voices', profile.voices.length > 8
      ? t('modelCatalog.media.voicesMore', { list: shown, n: profile.voices.length })
      : shown);
  }
  push('modelCatalog.media.audioFormats', list(profile.audio_formats));
  push('modelCatalog.media.maxInputChars',
    profile.max_input_chars ? profile.max_input_chars.toLocaleString() : null);
  push('modelCatalog.media.maxAudioBytes',
    profile.max_audio_bytes ? mebibytes(profile.max_audio_bytes) : null);
  return lines;
}
