/** Token presentation only; billing authority is lib/cost.py's normalize_usage /
 * split_input_tokens. Prefer each settled cost field independently, then usage.
 * Unlike billing's additive zero identity, missing display counts stay unknown.
 * Omitted optional cache buckets contribute zero ONLY when splitting a known
 * input count; cache-only usage cannot establish the complete prompt size.
 */
type RecordValue = Record<string, unknown>;

function record(value: unknown): RecordValue {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : {};
}

function count(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && !value.trim()) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

// Gateway canonical zero placeholders must not mask a populated vendor alias.
// This mirrors normalize_usage; an explicit zero survives if no alias is > 0.
function alias(...values: unknown[]): number | null {
  const counts = values.map(count);
  return counts.find(value => value !== null && value > 0)
    ?? counts.find(value => value !== null) ?? null;
}

export function normalizeTokenUsage(usage: unknown, cost?: unknown) {
  const u = record(usage);
  const c = record(cost);
  const read = alias(u.cache_read_tokens, u.cache_read_input_tokens,
    u.cached_tokens, u.effectiveCachedTokens, u.prompt_cache_hit_tokens,
    u.cached_content_token_count, record(u.prompt_tokens_details).cached_tokens);
  const write = alias(u.cache_write_tokens, u.cache_creation_input_tokens);
  const prompt = count(u.prompt_tokens);
  const native = count(u.input_tokens);
  const cached = (read ?? 0) + (write ?? 0);
  // Strict > is essential: equality is a fully cached OpenAI prompt, NOT an
  // Anthropic residual. Native cache keys take precedence on hybrid gateways;
  // when both input spellings exist, input_tokens is the native residual.
  const anthropic = u.cache_read_input_tokens != null
    || u.cache_creation_input_tokens != null
    || (prompt !== null ? cached > prompt : native !== null);
  const input = anthropic ? (native ?? prompt) : (prompt ?? native);
  const total = input === null ? null : anthropic ? input + cached : input;
  const uncached = input === null ? null : anthropic ? input : Math.max(0, input - cached);
  return {
    totalInputTokens: count(c.totalInputTokens) ?? total,
    inputTokens: count(c.inputTokens) ?? uncached,
    outputTokens: count(c.outputTokens) ?? alias(u.completion_tokens, u.output_tokens),
    cacheReadTokens: count(c.cacheReadTokens) ?? read,
    cacheWriteTokens: count(c.cacheWriteTokens) ?? write,
    thinkingTokens: alias(u.reasoning_tokens, u.thinking_tokens,
      record(u.completion_tokens_details).reasoning_tokens),
  };
}
