/** Own ephemeral preview indexes; merge changed blocks without copying history.
 * Durable Turn/DTO inputs are borrowed and never mutated. Joined narration is
 * derived lazily when a scheduled presentation reads it, not per push frame.
 */
import type { TranslationActivity } from '../presentation/conversation-view-model';

const ownedPreview = Symbol('translation-preview-owner');
type PreviewIndex = Record<string, string> & { [ownedPreview]: true };

function previewIndex(previous: TranslationActivity | undefined,
  incoming: Readonly<Record<string, string>>): PreviewIndex {
  const prior = previous?.status === 'pending' ? previous.partialByRound : undefined;
  const entries = Object.entries(incoming);
  const owned = prior && (prior as PreviewIndex)[ownedPreview];
  const index = owned && entries.length ? prior as PreviewIndex
    : Object.assign(Object.create(null), entries.length ? prior : undefined) as PreviewIndex;
  Object.defineProperty(index, ownedPreview, { value: true, configurable: true });
  for (const [key, value] of entries) {
    if (typeof value === 'string') index[key] = value;
  }
  return index;
}

function narrationKeyOrder(key: string): [number, number, string] {
  if (/^\d+$/.test(key)) return [0, Number(key), key];
  const round = /:llm-(\d+)(?::|$)/.exec(key);
  return round ? [0, Number(round[1]), key] : [1, 0, key];
}

export function mergeTranslationActivity(previous: TranslationActivity | undefined,
  incoming: TranslationActivity): TranslationActivity {
  if (incoming.status !== 'pending') return { ...incoming };
  const prior = previous?.status === 'pending' ? previous : undefined;
  const next: TranslationActivity = {
    status: incoming.status,
    message: incoming.message ?? prior?.message,
    error: incoming.error ?? prior?.error,
  };
  const changed = incoming.partialByRound;
  const index = changed !== undefined ? previewIndex(prior, changed) : prior?.partialByRound;
  if (index !== undefined) next.partialByRound = index;
  if (incoming.partial !== undefined) next.partial = incoming.partial;
  else if (changed === undefined && prior?.partial !== undefined) next.partial = prior.partial;
  else if (index !== undefined) {
    let joined: string | undefined;
    Object.defineProperty(next, 'partial', { enumerable: true, get() {
      if (joined === undefined) {
        joined = Object.keys(index).filter(key => !key.startsWith('thinking:'))
          .sort((left, right) => {
            const a = narrationKeyOrder(left), b = narrationKeyOrder(right);
            return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0);
          }).map(key => index[key]).filter(value => value.trim()).join('\n\n');
      }
      return joined;
    } });
  }
  return next;
}
