import { describe, expect, it } from 'vitest';
import { normalizeTokenUsage } from './token-usage';

describe('nullable token presentation (billing authority: lib/cost.py)', () => {
  const usage = {
    input_tokens: 60, output_tokens: 200,
    cache_read_input_tokens: 8448, cache_creation_input_tokens: 27392,
  };
  const split = { totalInputTokens: 35900, inputTokens: 60,
    outputTokens: 200, cacheReadTokens: 8448, cacheWriteTokens: 27392 };

  it.each([undefined, null, {}, { costCny: 0.4 },
    { totalInputTokens: 35900 }, { inputTokens: 60 }, split])(
    'falls back per field for full/partial/pending cost %j', cost => {
      expect(normalizeTokenUsage(usage, cost)).toMatchObject(split);
    });

  it('prefers explicit settled counts, including zero, independently', () => {
    expect(normalizeTokenUsage(usage, { totalInputTokens: 0, inputTokens: 0,
      outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 })).toMatchObject({
      totalInputTokens: 0, inputTokens: 0, outputTokens: 0,
      cacheReadTokens: 0, cacheWriteTokens: 0,
    });
    expect(normalizeTokenUsage({}, { totalInputTokens: 35900, inputTokens: 60 }))
      .toMatchObject({ totalInputTokens: 35900, inputTokens: 60, outputTokens: null });
  });

  it.each([undefined, null, {}, [], { output_tokens: 0 }])(
    'never invents input for absent usage %j', value => {
      expect(normalizeTokenUsage(value)).toMatchObject({
        totalInputTokens: null, inputTokens: null, cacheReadTokens: null,
      });
    });

  it('distinguishes cache-only and explicit zero usage', () => {
    expect(normalizeTokenUsage({ cache_read_input_tokens: 800 }))
      .toMatchObject({ totalInputTokens: null, inputTokens: null, cacheReadTokens: 800 });
    expect(normalizeTokenUsage({ prompt_tokens: 0, completion_tokens: 0,
      cache_read_tokens: 0, cache_write_tokens: 0 })).toMatchObject({
      totalInputTokens: 0, inputTokens: 0, outputTokens: 0,
      cacheReadTokens: 0, cacheWriteTokens: 0,
    });
  });

  it.each([
    [{ prompt_tokens: 1000, cached_tokens: 900 }, 1000, 100],
    [{ input_tokens: 100, cache_read_input_tokens: 900 }, 1000, 100],
    [{ prompt_tokens: 100, cache_read_tokens: 900 }, 1000, 100],
    [{ prompt_tokens: 1000, input_tokens: 100, cache_read_input_tokens: 900 }, 1000, 100],
    [{ prompt_tokens: 1000, input_tokens: 0, cache_read_input_tokens: 1000 }, 1000, 0],
    [{ prompt_tokens: 1000, cache_read_tokens: 1000 }, 1000, 0],
    [{ prompt_tokens: 1000, cache_read_tokens: 900, cache_write_tokens: 100 }, 1000, 0],
    [{ prompt_tokens: 1000, cache_read_tokens: 1001 }, 2001, 1000],
  ])('splits OpenAI/Anthropic/hybrid and the equality boundary: %j', (raw, total, uncached) => {
    expect(normalizeTokenUsage(raw)).toMatchObject({ totalInputTokens: total, inputTokens: uncached });
  });

  it.each(['cached_tokens', 'effectiveCachedTokens', 'prompt_cache_hit_tokens',
    'cached_content_token_count'])('does not mask vendor alias %s with canonical zero', key => {
    expect(normalizeTokenUsage({ prompt_tokens: 1000, cache_read_tokens: 0, [key]: 900 }))
      .toMatchObject({ totalInputTokens: 1000, inputTokens: 100, cacheReadTokens: 900 });
  });

  it('reads nested OpenAI usage and reasoning counters', () => {
    expect(normalizeTokenUsage({ prompt_tokens: 1000, prompt_tokens_details: { cached_tokens: 1000 },
      completion_tokens: 20, completion_tokens_details: { reasoning_tokens: 10 } }))
      .toMatchObject({ totalInputTokens: 1000, inputTokens: 0, cacheReadTokens: 1000,
        outputTokens: 20, thinkingTokens: 10 });
  });

  it.each([null, '', ' ', false, -1, NaN, Infinity, {}, 1.5])(
    'rejects invalid counts rather than coercing %j to zero', invalid => {
      expect(normalizeTokenUsage({ input_tokens: invalid }, { totalInputTokens: invalid }))
        .toMatchObject({ totalInputTokens: null, inputTokens: null });
      expect(normalizeTokenUsage(usage, { inputTokens: invalid }).inputTokens).toBe(60);
    });
});
