/**
 * Responsibility: bounded, endpoint-backed compaction-history state.
 * Entry point: createCompactionHistoryState. Dependencies: injected list,
 * clock, and refresh-notification ports; no DOM or application globals.
 *
 * The archive listing is the count authority. Loaded Turns reference archives
 * through compaction receipts; `observe` lists again when they reference one
 * no settled listing has covered, so an automatic compaction in an open
 * conversation reaches the count without polling.
 */

export const COMPACTION_HISTORY_LIMITS = Object.freeze({
  conversations: 32,
  rowsPerConversation: 64,
  inflight: 32,
  freshMs: 15_000,
});

export interface CompactionHistoryRow {
  readonly schemaVersion: string;
  readonly archiveId: string;
  readonly convId: string;
  readonly snapshotKind: string;
  readonly trigger: string;
  readonly roundNum: number;
  readonly tokensBefore: number;
  readonly tokensAfter: number;
  readonly tokenCountKind: string;
  readonly msgsBefore: number;
  readonly msgsAfter: number;
  readonly model: string;
  readonly taskModel: string;
  readonly reason: string;
  readonly payloadSize: number;
  readonly payloadSizeUnit: string;
  readonly summaryPreview: string;
  readonly hasSummary: boolean;
  readonly hasReceipt: boolean;
  readonly resultStatus: string;
  readonly resultStrategy: string;
  readonly ts: number;
  readonly status: 'done';
}

export interface CompactionListPayload {
  readonly compactions: readonly unknown[];
  readonly [key: string]: unknown;
}

export interface CompactionHistoryState {
  /** Listed archive count; null until a listing for the conversation settles. */
  readonly count: (conversationId: unknown) => number | null;
  readonly get: (conversationId: unknown) => readonly CompactionHistoryRow[];
  readonly list: (
    conversationId: unknown,
    options?: Readonly<{ force?: boolean }>,
  ) => Promise<CompactionListPayload>;
  readonly refresh: (
    conversationId: unknown,
  ) => Promise<readonly CompactionHistoryRow[]>;
  /**
   * Reconcile with the archive IDs the conversation's loaded Turns reference.
   * A listing covers the IDs observed when it starts, so an ID the endpoint
   * never lists (child scope, pruned, failed-attempt placeholder) costs at
   * most one listing; only a newly referenced ID lists again.
   */
  readonly observe: (
    conversationId: unknown,
    archiveIds: readonly string[],
  ) => void;
  readonly limits: typeof COMPACTION_HISTORY_LIMITS;
}

export interface CompactionHistoryStatePorts {
  readonly list: (conversationId: string) => unknown | PromiseLike<unknown>;
  readonly now?: () => number;
  readonly onRefresh?: () => void;
}

interface HistoryRecord {
  readonly loadedAt: number;
  readonly history: readonly CompactionHistoryRow[];
  readonly totalCount: number;
  readonly archiveIds: ReadonlySet<string>;
}

interface ConversationHistory {
  record: HistoryRecord | null;
  /** Archive IDs the loaded Turns referenced at the latest observation. */
  observed: readonly string[] | null;
  /** Observed IDs when the latest listing started; null before any listing. */
  covered: ReadonlySet<string> | null;
}

const EMPTY_HISTORY: readonly CompactionHistoryRow[] = Object.freeze([]);
const EMPTY_PAYLOAD: CompactionListPayload = Object.freeze({
  compactions: EMPTY_HISTORY,
});

function objectValue(value: unknown): Readonly<Record<string, unknown>> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>> : Object.freeze({});
}

function textValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function numberValue(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function normalizePayload(value: unknown): CompactionListPayload {
  const source = objectValue(value);
  if (Array.isArray(source.compactions)) {
    return source as CompactionListPayload;
  }
  return Object.freeze({ ...source, compactions: EMPTY_HISTORY });
}

function projectHistory(
  conversationId: string,
  payload: CompactionListPayload,
): readonly CompactionHistoryRow[] {
  const rows = payload.compactions.map((value) => {
    const archive = objectValue(value);
    const model = textValue(archive.model);
    return Object.freeze({
      schemaVersion: textValue(archive.schemaVersion),
      archiveId: textValue(archive.id),
      convId: textValue(archive.convId, conversationId),
      snapshotKind: textValue(archive.snapshotKind),
      trigger: textValue(archive.trigger, 'force'),
      roundNum: numberValue(archive.roundNum),
      tokensBefore: numberValue(archive.tokensBefore),
      tokensAfter: numberValue(archive.tokensAfter),
      tokenCountKind: textValue(archive.tokenCountKind),
      msgsBefore: numberValue(archive.msgsBefore),
      msgsAfter: numberValue(archive.msgsAfter),
      model,
      taskModel: textValue(archive.taskModel, model),
      reason: textValue(archive.reason),
      payloadSize: numberValue(archive.payloadSize),
      payloadSizeUnit: textValue(archive.payloadSizeUnit),
      summaryPreview: textValue(archive.summaryPreview),
      hasSummary: Boolean(archive.hasSummary),
      hasReceipt: Boolean(archive.hasReceipt),
      resultStatus: textValue(archive.resultStatus, 'legacy'),
      resultStrategy: textValue(archive.resultStrategy),
      ts: numberValue(archive.createdAt),
      status: 'done' as const,
    });
  });
  rows.sort((left, right) => left.ts - right.ts);
  return Object.freeze(
    rows.slice(-COMPACTION_HISTORY_LIMITS.rowsPerConversation),
  );
}

export function createCompactionHistoryState(
  ports: CompactionHistoryStatePorts,
): CompactionHistoryState {
  const now = ports.now ?? Date.now;
  const conversations = new Map<string, ConversationHistory>();
  const inflight = new Map<string, Promise<CompactionListPayload>>();

  /** Touch an existing conversation without admitting a new one. */
  const existing = (conversationId: string): ConversationHistory | undefined => {
    const conversation = conversations.get(conversationId);
    if (conversation) {
      conversations.delete(conversationId);
      conversations.set(conversationId, conversation);
    }
    return conversation;
  };

  const admit = (conversationId: string): ConversationHistory => {
    const conversation = existing(conversationId)
      ?? { record: null, observed: null, covered: null };
    conversations.set(conversationId, conversation);
    while (conversations.size > COMPACTION_HISTORY_LIMITS.conversations) {
      const oldest = conversations.keys().next().value;
      if (oldest === undefined) break;
      conversations.delete(oldest);
    }
    return conversation;
  };

  const store = (
    conversationId: string,
    payload: CompactionListPayload,
  ): void => {
    admit(conversationId).record = Object.freeze({
      loadedAt: now(),
      history: projectHistory(conversationId, payload),
      totalCount: payload.compactions.length,
      archiveIds: new Set(payload.compactions.flatMap((value) => {
        const id = objectValue(value).id;
        return typeof id === 'string' && id ? [id] : [];
      })),
    });
  };

  const get = (conversationId: unknown): readonly CompactionHistoryRow[] => {
    const key = String(conversationId || '');
    return (key && existing(key)?.record?.history) || EMPTY_HISTORY;
  };

  const count = (conversationId: unknown): number | null => {
    const key = String(conversationId || '');
    const record = key ? existing(key)?.record : null;
    return record ? record.totalCount : null;
  };

  const reconcile = (key: string): void => {
    const conversation = conversations.get(key);
    const observed = conversation?.observed;
    if (!conversation || !observed || inflight.has(key)) return;
    const { covered, record } = conversation;
    if (covered && observed.every((id) => (
      covered.has(id) || record?.archiveIds.has(id)
    ))) return;
    list(key, { force: true }).then(() => ports.onRefresh?.(), () => undefined);
  };

  const list = async (
    conversationId: unknown,
    options?: Readonly<{ force?: boolean }>,
  ): Promise<CompactionListPayload> => {
    const key = String(conversationId || '');
    if (!key) return EMPTY_PAYLOAD;
    const pending = inflight.get(key);
    if (pending) return pending;

    const record = existing(key)?.record;
    if (!options?.force && record
        && now() - record.loadedAt < COMPACTION_HISTORY_LIMITS.freshMs) {
      return Object.freeze({ compactions: record.history });
    }

    const conversation = admit(key);
    conversation.covered = new Set(conversation.observed ?? []);
    const request = Promise.resolve()
      .then(() => ports.list(key))
      .then(normalizePayload)
      .then((payload) => {
        store(key, payload);
        return payload;
      });
    const tracked = inflight.size < COMPACTION_HISTORY_LIMITS.inflight;
    if (tracked) inflight.set(key, request);
    try {
      return await request;
    } finally {
      if (tracked && inflight.get(key) === request) inflight.delete(key);
      reconcile(key);
    }
  };

  const refresh = async (
    conversationId: unknown,
  ): Promise<readonly CompactionHistoryRow[]> => {
    const key = String(conversationId || '');
    if (!key) return EMPTY_HISTORY;
    await list(key);
    ports.onRefresh?.();
    return get(key);
  };

  const observe = (
    conversationId: unknown,
    archiveIds: readonly string[],
  ): void => {
    const key = String(conversationId || '');
    if (!key) return;
    admit(key).observed = archiveIds.filter(
      (id) => typeof id === 'string' && id.length > 0,
    );
    reconcile(key);
  };

  return Object.freeze({
    count,
    get,
    list,
    refresh,
    observe,
    limits: COMPACTION_HISTORY_LIMITS,
  });
}
