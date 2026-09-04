/**
 * Responsibility: bounded, endpoint-backed compaction-history state.
 * Entry point: createCompactionHistoryState. Dependencies: injected list,
 * clock, and refresh-notification ports; no DOM or application globals.
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
  readonly count: (conversationId: unknown) => number;
  readonly get: (conversationId: unknown) => readonly CompactionHistoryRow[];
  readonly list: (
    conversationId: unknown,
    options?: Readonly<{ force?: boolean }>,
  ) => Promise<CompactionListPayload>;
  readonly refresh: (
    conversationId: unknown,
  ) => Promise<readonly CompactionHistoryRow[]>;
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
  const records = new Map<string, HistoryRecord>();
  const inflight = new Map<string, Promise<CompactionListPayload>>();

  const touch = (conversationId: string, record: HistoryRecord): void => {
    records.delete(conversationId);
    records.set(conversationId, record);
  };

  const store = (
    conversationId: string,
    payload: CompactionListPayload,
  ): void => {
    const record = Object.freeze({
      loadedAt: now(),
      history: projectHistory(conversationId, payload),
      totalCount: payload.compactions.length,
    });
    touch(conversationId, record);
    while (records.size > COMPACTION_HISTORY_LIMITS.conversations) {
      const oldest = records.keys().next().value;
      if (oldest === undefined) break;
      records.delete(oldest);
    }
  };

  const get = (conversationId: unknown): readonly CompactionHistoryRow[] => {
    const key = String(conversationId || '');
    const record = key ? records.get(key) : undefined;
    if (!record) return EMPTY_HISTORY;
    touch(key, record);
    return record.history;
  };

  const count = (conversationId: unknown): number => {
    const key = String(conversationId || '');
    const record = key ? records.get(key) : undefined;
    if (!record) return 0;
    touch(key, record);
    return record.totalCount;
  };

  const list = async (
    conversationId: unknown,
    options?: Readonly<{ force?: boolean }>,
  ): Promise<CompactionListPayload> => {
    const key = String(conversationId || '');
    if (!key) return EMPTY_PAYLOAD;
    const pending = inflight.get(key);
    if (pending) return pending;

    const record = records.get(key);
    if (!options?.force && record
        && now() - record.loadedAt < COMPACTION_HISTORY_LIMITS.freshMs) {
      touch(key, record);
      return Object.freeze({ compactions: record.history });
    }

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

  return Object.freeze({
    count,
    get,
    list,
    refresh,
    limits: COMPACTION_HISTORY_LIMITS,
  });
}
