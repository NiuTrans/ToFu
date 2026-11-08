import { describe, expect, it, vi } from 'vitest';

// Copy is the i18n contract's job; these specs pin which key/params a fact
// renders through, with a translator that substitutes {placeholders}.
vi.mock('../../i18n', () => ({
  t: (key: string, params?: Record<string, unknown>) => {
    const copy: Record<string, string> = {
      'modelPriceUnit.per_image': '{price}/image',
      'modelPriceUnit.per_second': '{price}/s',
      'modelPriceUnit.per_request': '{price}/request',
      'modelCatalog.media.from': 'from {price}',
      'modelCatalog.media.seconds': '{list} s',
      'modelCatalog.media.voicesMore': '{list} · {n} total',
      'modelCatalog.media.yes': 'Yes',
    };
    const text = copy[key] ?? key;
    return text.replace(/\{(\w+)\}/g, (token, name) => (
      params && name in params ? String(params[name]) : token));
  },
}));

import {
  capabilityLabel,
  formatMediaRate,
  kindGlyph,
  kindLabel,
  mediaFactLines,
  mediaPriceSummary,
  modalityFlow,
} from './kinds';

const usd = (value: number) => `$${value}`;

describe('model kind presentation', () => {
  it('labels known ids through their copy family and keeps unknown ids raw', () => {
    expect(kindLabel('video_generation')).toBe('modelKind.video_generation');
    expect(capabilityLabel('audio_gen')).toBe('modelCapability.audio_gen');
    expect(kindLabel('hologram_generation')).toBe('hologram_generation');
    expect(capabilityLabel('future_tag')).toBe('future_tag');
  });

  it('draws a glyph for every kind, including unknown ones', () => {
    expect(kindGlyph('speech_synthesis')).toMatch(/^<svg[^>]*aria-hidden="true"/);
    expect(kindGlyph('hologram_generation')).toContain('<circle');
  });

  it('marks creator estimates and keeps unknown units legible', () => {
    expect(formatMediaRate({ unit: 'per_image', price: 0.04 }, usd)).toBe('$0.04/image');
    expect(formatMediaRate({ unit: 'per_second', price: 0.07, estimated: true }, usd)).toBe('≈$0.07/s');
    expect(formatMediaRate({ unit: 'per_hologram' as never, price: 1 }, usd)).toBe('$1/per_hologram');
  });

  it('summarizes the cheapest rate per unit and says when tiers differ', () => {
    expect(mediaPriceSummary(null, usd)).toBeNull();
    expect(mediaPriceSummary({ currency: 'USD', rates: [
      { unit: 'per_image', price: 0.08, tier: 'hd' },
      { unit: 'per_image', price: 0.04, tier: 'standard' },
    ] }, usd)).toBe('from $0.04/image');
    expect(mediaPriceSummary({ currency: 'USD', rates: [
      { unit: 'per_second', price: 0.4 },
      { unit: 'per_request', price: 0.08 },
    ] }, usd)).toBe('$0.4/s · $0.08/request');
  });

  it('renders the modality flow and only documented profile facts', () => {
    expect(modalityFlow({ input_modalities: ['text', 'image'], output_modalities: ['video'] }))
      .toBe('modelModality.text + modelModality.image → modelModality.video');
    expect(modalityFlow({ output_modalities: ['audio'] })).toBe('— → modelModality.audio');
    expect(modalityFlow({})).toBeNull();
    const voices = Array.from({ length: 10 }, (_, index) => `v${index}`);
    const lines = mediaFactLines({
      durations_s: [4, 6, 8], native_audio: true, voices, max_audio_bytes: 26214400,
    });
    expect(lines).toEqual([
      { label: 'modelCatalog.media.durations', value: '4 / 6 / 8 s' },
      { label: 'modelCatalog.media.nativeAudio', value: 'Yes' },
      { label: 'modelCatalog.media.voices', value: 'v0, v1, v2, v3, v4, v5, v6, v7 · 10 total' },
      { label: 'modelCatalog.media.maxAudioBytes', value: '25 MiB' },
    ]);
    expect(mediaFactLines(null)).toEqual([]);
  });
});
