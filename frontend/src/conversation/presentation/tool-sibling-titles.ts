/** Pure within-batch sibling-title derivation; grouping is supplied, so render passes never regroup the timeline. */
type UnknownRecord = Readonly<Record<string, unknown>>;
function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : null;
}
const _DISCRIM_VALUE_MAX = 24;

function _discrimArgs(round: unknown): UnknownRecord | null {
  const raw = record(round)?.toolArgs;
  if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as UnknownRecord;
  }
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as UnknownRecord;
      }
    } catch { /* malformed toolArgs — occurrence index still applies */ }
  }
  return null;
}

function _discrimValue(value: unknown): string {
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text) return '';
  return text.length > _DISCRIM_VALUE_MAX
    ? `${text.slice(0, _DISCRIM_VALUE_MAX - 1)}…` : text;
}

/**
 * Guarantee no two sibling rows in one batch ever render the SAME title.
 *
 * The backend composes each round's `query` per call, unaware of siblings;
 * a parallel batch can therefore legitimately contain several calls whose
 * composed titles collide (same tool, same resource, differing only in args
 * the title elided — or, for a future tool, args no key list knows about).
 * For every within-batch cluster of rounds sharing (toolName, query), derive
 * a suffix from the args that ACTUALLY differ across the cluster
 * (` · key=value`, at most two keys present in every sibling); when the args
 * are byte-equal too, fall back to the occurrence index (` #2`, ` #3`).
 * Keyed by durable toolCallId so callers never depend on object identity.
 */
export function deriveSiblingTitleDiscriminators(
  batches: readonly { readonly rounds: readonly unknown[] }[],
): ReadonlyMap<string, string> {
  const result = new Map<string, string>();
  for (const batch of batches) {
    const clusters = new Map<string, unknown[]>();
    for (const round of batch.rounds) {
      const direct = record(round);
      const callId = direct?.toolCallId;
      const name = direct?.toolName;
      const query = direct?.query;
      if (typeof callId !== 'string' || !callId) continue;
      if (typeof name !== 'string' || !name) continue;
      if (typeof query !== 'string' || !query) continue;
      const key = JSON.stringify([name, query]);
      const cluster = clusters.get(key);
      if (cluster) cluster.push(round);
      else clusters.set(key, [round]);
    }
    for (const cluster of clusters.values()) {
      if (cluster.length < 2) continue;
      const argsList = cluster.map(_discrimArgs);
      const keyOrder: string[] = [];
      for (const args of argsList) {
        if (!args) continue;
        for (const key of Object.keys(args)) {
          if (!keyOrder.includes(key)) keyOrder.push(key);
        }
      }
      const rendered = argsList.map((args) => {
        const valuesByKey = new Map<string, string>();
        if (args) {
          for (const key of keyOrder) {
            const text = _discrimValue(args[key]);
            if (text) valuesByKey.set(key, text);
          }
        }
        return valuesByKey;
      });
      const diffKeys = keyOrder.filter((key) => {
        const distinct = new Set<string>();
        for (const valuesByKey of rendered) {
          const text = valuesByKey.get(key);
          if (text === undefined) return false;
          distinct.add(text);
        }
        return distinct.size > 1;
      }).slice(0, 2);
      const suffixes = cluster.map((round, index) => {
        if (!diffKeys.length) return index > 0 ? ` #${index + 1}` : '';
        const chips = diffKeys
          .map((key) => `${key}=${rendered[index].get(key) ?? ''}`);
        return ` · ${chips.join(' · ')}`;
      });
      /* Residual collisions: two siblings whose args agree on every diff key
       * (≤2 of possibly several differing keys) still render identically —
       * append the within-suffix occurrence index so the guarantee holds. */
      const seen = new Map<string, number>();
      cluster.forEach((round, index) => {
        let suffix = suffixes[index];
        const occurrence = (seen.get(suffix) ?? 0) + 1;
        seen.set(suffix, occurrence);
        if (occurrence > 1) suffix = `${suffix} #${occurrence}`;
        if (suffix) result.set(String(record(round)?.toolCallId), suffix);
      });
    }
  }
  return result;
}
