/** Browser-local review receipts for canonical completed replies.
 * Entry points: observe for sidebar projection, viewed after a Surface commit.
 * Execution truth stays in TurnStore; receipts are scoped by authenticated owner.
 */
import type { TurnRecord } from './api/conversation-sync.generated';

interface Receipt {
  token: string;
  ordinal: number;
  updatedAt: number;
  viewed: boolean;
}
interface ReviewHost {
  ownerId(): number | null;
  storage(): Pick<Storage, 'getItem' | 'setItem'> | undefined;
  canView(): boolean;
}
const PREFIX = 'tofu:conversation-review:v1:';

function completion(conversationId: string, turns: ReadonlyArray<TurnRecord>) {
  const latest = turns.filter(turn => turn.conversationId === conversationId
    && turn.laneId === 'main' && ['assistant', 'planner'].includes(turn.actor))
    .reduce<TurnRecord | undefined>((previous, turn) =>
      !previous || turn.ordinal >= previous.ordinal ? turn : previous, undefined);
  if (!latest || latest.status !== 'completed' || latest.settlement?.error
      || latest.projection.error || latest.projection.waitingOn) return null;
  return {
    turnId: latest.turnId,
    token: JSON.stringify([latest.turnId, latest.currentAttemptId || '']),
    ordinal: latest.ordinal, updatedAt: latest.updatedAt, viewed: false,
  };
}

export function createConversationReviewTracker(host: ReviewHost) {
  // One small receipt per encountered catalog row; no transcript, timer or request.
  const receipts = new Map<string, Receipt | null>();
  let owner: number | null = null;
  const key = (id: string): string | null => {
    const next = host.ownerId();
    if (next !== owner) { receipts.clear(); owner = next; }
    return Number.isSafeInteger(owner) && Number(owner) > 0
      ? `${PREFIX}${owner}:${encodeURIComponent(id)}` : null;
  };
  function read(storageKey: string): Receipt | null {
    if (receipts.has(storageKey)) return receipts.get(storageKey) || null;
    let value: Receipt | null = null;
    try {
      const parsed = JSON.parse(host.storage()?.getItem(storageKey) || 'null');
      if (parsed && typeof parsed.token === 'string' && parsed.token.length <= 1024
          && Number.isSafeInteger(parsed.ordinal) && parsed.ordinal >= 0
          && Number.isFinite(parsed.updatedAt) && typeof parsed.viewed === 'boolean') value = parsed;
    } catch { /* Storage may be unavailable or contain an obsolete receipt. */ }
    receipts.set(storageKey, value);
    return value;
  }
  function write(storageKey: string, value: Receipt) {
    receipts.set(storageKey, value);
    try { host.storage()?.setItem(storageKey, JSON.stringify(value)); }
    catch { /* Keep the current tab's receipt when browser storage is unavailable. */ }
  }
  function observe(id: string, turns: ReadonlyArray<TurnRecord>) {
    const storageKey = key(id);
    if (!storageKey) return false;
    let previous = read(storageKey);
    const next = completion(id, turns);
    if (next && next.token !== previous?.token
        && (!previous || next.ordinal > previous.ordinal
          || (next.ordinal === previous.ordinal && next.updatedAt >= previous.updatedAt))) {
      // Re-read before a write so a second tab's acknowledgement wins a race.
      receipts.delete(storageKey);
      const stored = read(storageKey);
      if (stored?.token === next.token) previous = stored;
      else if (!stored || next.ordinal > stored.ordinal
          || (next.ordinal === stored.ordinal && next.updatedAt >= stored.updatedAt)) {
        const { token, ordinal, updatedAt, viewed } = next;
        previous = { token, ordinal, updatedAt, viewed };
        write(storageKey, previous);
      } else previous = stored;
    }
    return Boolean(previous && !previous.viewed);
  }
  return {
    observe,
    viewed(id: string, turns: ReadonlyArray<TurnRecord>, mounted: ReadonlyArray<string>) {
      if (!host.canView()) return false;
      const next = completion(id, turns);
      if (!next || !mounted.includes(next.turnId)) return false;
      observe(id, turns);
      const storageKey = key(id);
      // Do not replace a newer completion another tab stored before its event arrived.
      const cached = storageKey && read(storageKey);
      if (storageKey) receipts.delete(storageKey);
      const receipt = (storageKey && read(storageKey)) || cached;
      if (storageKey && receipt) receipts.set(storageKey, receipt);
      if (!storageKey || !receipt || receipt.token !== next.token || receipt.viewed) return false;
      write(storageKey, { ...receipt, viewed: true });
      return true;
    },
    storageChanged(storageKey: string | null) {
      if (storageKey !== null && !storageKey.startsWith(PREFIX)) return false;
      if (storageKey) receipts.delete(storageKey); else receipts.clear();
      return true;
    },
    destroy() { receipts.clear(); },
  };
}
