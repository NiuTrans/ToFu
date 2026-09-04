/**
 * Disposable My Day background lifecycle.
 *
 * Responsibility: perform one cache-first day-digest revalidation and schedule
 * at most one owner-scoped afternoon reminder. Entry point:
 * `createMyDayBackgroundController`. Dependencies: injected repository, API,
 * clock, timer, storage, visibility and notification ports.
 */

import type { MyDayReport } from './model';
import {
  myDayLocalDateKey,
  type MyDayReportRepository,
} from './report-cache';

export const MYDAY_REMINDER_DELAY_MS = 3 * 60 * 60 * 1000;
export const MYDAY_REMINDER_MINIMUM_HOUR = 14;
export const MYDAY_REMINDER_MINIMUM_CONVERSATIONS = 3;
export const MYDAY_REMINDER_MAX_OWNER_ENTRIES = 16;
export const MYDAY_REMINDER_STORAGE_KEY = 'tofu_myday_reminders_v2';

interface MyDayStatusResponse {
  readonly status?: unknown;
  readonly report?: unknown;
}

interface MyDayConversationCountResponse {
  readonly count?: unknown;
}

export interface MyDayReminderNotice {
  readonly icon: string;
  readonly title: string;
  readonly body: string;
  readonly durationMs: number;
}

export interface MyDayBackgroundPorts {
  readonly ownerId: () => number | null;
  readonly repository: MyDayReportRepository;
  readonly readStatus: (date: string) => Promise<MyDayStatusResponse | null>;
  readonly readConversationCount: (
    date: string,
  ) => Promise<MyDayConversationCountResponse | null>;
  readonly notify: (notice: MyDayReminderNotice) => boolean | void;
  readonly translate: (key: string, values?: Record<string, unknown>) => string;
  readonly storage?: Pick<Storage, 'getItem' | 'setItem'>;
  readonly reportIsOpen: () => boolean;
  readonly now?: () => Date;
  readonly setTimeout?: (callback: () => void, delayMs: number) => unknown;
  readonly clearTimeout?: (handle: unknown) => void;
  readonly warn?: (message: string, detail?: unknown) => void;
}

export interface MyDayBackgroundController {
  readonly source: 'typed';
  start(): void;
  refreshDigest(): Promise<void>;
  checkReminder(): Promise<void>;
  destroy(): void;
}

interface ReminderLedgerEntry {
  readonly ownerId: number;
  readonly date: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function report(value: unknown): MyDayReport | null {
  return record(value) as MyDayReport | null;
}

function readLedger(
  storage: MyDayBackgroundPorts['storage'],
): ReminderLedgerEntry[] {
  if (!storage) return [];
  try {
    const parsed: unknown = JSON.parse(storage.getItem(MYDAY_REMINDER_STORAGE_KEY) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((value) => {
      const entry = record(value);
      const ownerId = Number(entry?.ownerId);
      const date = typeof entry?.date === 'string' ? entry.date : '';
      return Number.isSafeInteger(ownerId) && ownerId > 0
        && /^\d{4}-\d{2}-\d{2}$/.test(date)
        ? [{ ownerId, date }] : [];
    }).slice(-MYDAY_REMINDER_MAX_OWNER_ENTRIES);
  } catch {
    return [];
  }
}

function writeLedger(
  storage: MyDayBackgroundPorts['storage'],
  entries: readonly ReminderLedgerEntry[],
): void {
  if (!storage) return;
  try {
    storage.setItem(
      MYDAY_REMINDER_STORAGE_KEY,
      JSON.stringify(entries.slice(-MYDAY_REMINDER_MAX_OWNER_ENTRIES)),
    );
  } catch {
    // Sandboxed/private browser storage is an optional reminder capability.
  }
}

export function createMyDayBackgroundController(
  ports: MyDayBackgroundPorts,
): MyDayBackgroundController {
  const now = ports.now ?? (() => new Date());
  const schedule = ports.setTimeout
    ?? ((callback, delayMs) => globalThis.setTimeout(callback, delayMs));
  const cancel = ports.clearTimeout
    ?? ((handle) => globalThis.clearTimeout(handle as number));

  let started = false;
  let destroyed = false;
  let digestFinished = false;
  let digestTimer: unknown = null;
  let reminderTimer: unknown = null;

  const refreshDigest = async (): Promise<void> => {
    if (destroyed || digestFinished) return;
    digestFinished = true;
    const date = myDayLocalDateKey(now());
    try {
      const cached = await ports.repository.readReport(date);
      if (destroyed) return;
      if (cached) ports.repository.publishReport(date, cached);
      const response = await ports.readStatus(date);
      if (destroyed) return;
      const freshReport = response?.status === 'done' ? report(response.report) : null;
      if (freshReport) await ports.repository.storeReport(date, freshReport);
    } catch (error) {
      ports.warn?.('[MyDay] background digest refresh failed', error);
    }
  };

  const checkReminder = async (): Promise<void> => {
    if (destroyed) return;
    const ownerId = ports.ownerId();
    const current = now();
    if (ownerId === null || current.getHours() < MYDAY_REMINDER_MINIMUM_HOUR
        || ports.reportIsOpen()) return;
    const date = myDayLocalDateKey(current);
    const ledger = readLedger(ports.storage);
    if (ledger.some((entry) => entry.ownerId === ownerId && entry.date === date)) return;
    try {
      const response = await ports.readConversationCount(date);
      if (destroyed) return;
      const rawCount = response?.count;
      const count = typeof rawCount === 'number' && Number.isFinite(rawCount)
        ? Math.max(0, Math.floor(rawCount)) : 0;
      if (count < MYDAY_REMINDER_MINIMUM_CONVERSATIONS) return;
      const shown = ports.notify({
        icon: '📋',
        title: ports.translate('myday.reminderTitle'),
        body: ports.translate('myday.reminderBody', { n: count }),
        durationMs: 8000,
      });
      if (shown === false) return;
      const nextLedger = ledger.filter((entry) => entry.ownerId !== ownerId);
      nextLedger.push({ ownerId, date });
      writeLedger(ports.storage, nextLedger);
    } catch (error) {
      ports.warn?.('[MyDay] reminder check failed', error);
    }
  };

  const start = (): void => {
    if (started || destroyed) return;
    started = true;
    // This owner itself is loaded from the idle background chunk. A zero-delay
    // task yields once more without adding another first-paint timeout.
    digestTimer = schedule(() => { void refreshDigest(); }, 0);
    reminderTimer = schedule(() => { void checkReminder(); }, MYDAY_REMINDER_DELAY_MS);
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    if (digestTimer !== null) cancel(digestTimer);
    if (reminderTimer !== null) cancel(reminderTimer);
    digestTimer = null;
    reminderTimer = null;
  };

  return Object.freeze({
    source: 'typed' as const,
    start,
    refreshDigest,
    checkReminder,
    destroy,
  });
}
