/**
 * Owner-scoped, bounded My Day read cache and report repository.
 *
 * Responsibility: isolate reconstructible daily-report cache entries by the
 * authenticated owner, enforce entry/byte ceilings, and publish the compact
 * day digest derived from an accepted report. Entry points:
 * `createMyDayPersistentCache`, `createMyDayReportRepository`. Dependencies:
 * injected storage, identity, clock and digest ports; no application globals.
 */

import type { MyDayReport } from './model';

import { resolveBrowserIndexedDb } from '../../core/browser-storage';

export const MYDAY_CACHE_MAX_REPORTS = 96;
export const MYDAY_CACHE_MAX_MONTHS = 24;
export const MYDAY_CACHE_MAX_REPORT_BYTES = 512 * 1024;
export const MYDAY_CACHE_MAX_MONTH_BYTES = 128 * 1024;

const DATABASE_NAME = 'tofu_myday_cache';
const DATABASE_VERSION = 3;
const REPORT_STORE = 'reports';
const MONTH_STORE = 'months';
const TIMESTAMP_INDEX = 'cachedAt';

type MyDayCacheStoreName = typeof REPORT_STORE | typeof MONTH_STORE;

export interface MyDayMonthOverview {
  readonly [key: string]: unknown;
}

export interface MyDayCacheRecord {
  readonly key: string;
  readonly value: unknown;
  readonly cachedAt: number;
}

export interface MyDayCacheStorage {
  read(store: MyDayCacheStoreName, key: string): Promise<unknown | null>;
  write(
    store: MyDayCacheStoreName,
    record: MyDayCacheRecord,
    maximumEntries: number,
  ): Promise<void>;
}

export interface MyDayCacheResourceBudget {
  readonly maximumReports: number;
  readonly maximumMonths: number;
  readonly maximumReportBytes: number;
  readonly maximumMonthBytes: number;
  readonly maximumEstimatedBytes: number;
}

export interface MyDayPersistentCache {
  readReport(ownerId: number, date: string): Promise<MyDayReport | null>;
  writeReport(ownerId: number, date: string, report: MyDayReport): Promise<void>;
  readMonth(ownerId: number, month: string): Promise<MyDayMonthOverview | null>;
  writeMonth(
    ownerId: number,
    month: string,
    overview: MyDayMonthOverview,
  ): Promise<void>;
}

export interface MyDayDigest {
  readonly streams: {
    readonly total: number;
    readonly done: number;
    readonly blocked: number;
  };
  readonly todos: {
    readonly total: number;
    readonly done: number;
  };
  readonly convCount: number;
}

export interface MyDayReportRepository {
  readReport(date: string): Promise<MyDayReport | null>;
  storeReport(date: string, report: MyDayReport): Promise<void>;
  publishReport(date: string, report: MyDayReport): void;
  readMonth(month: string): Promise<MyDayMonthOverview | null>;
  storeMonth(month: string, overview: MyDayMonthOverview): Promise<void>;
}

export interface MyDayReportRepositoryPorts {
  readonly cache: MyDayPersistentCache;
  readonly ownerId: () => number | null;
  readonly today?: () => string;
  readonly publishDigest?: (digest: MyDayDigest) => void;
}

interface MyDayPersistentCachePorts {
  readonly storage?: MyDayCacheStorage;
  readonly now?: () => number;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function validOwnerId(ownerId: number): boolean {
  return Number.isSafeInteger(ownerId) && ownerId > 0;
}

function validDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function validMonth(value: string): boolean {
  return /^\d{4}-\d{2}$/.test(value);
}

function scopedKey(ownerId: number, kind: 'report' | 'month', key: string): string {
  return `owner:${ownerId}:${kind}:${key}`;
}

function serializedBytes(value: unknown): number | null {
  try {
    const serialized = JSON.stringify(value);
    if (typeof serialized !== 'string') return null;
    if (typeof TextEncoder === 'function') {
      return new TextEncoder().encode(serialized).byteLength;
    }
    return serialized.length * 4;
  } catch {
    return null;
  }
}

export function createIndexedDbMyDayCacheStorage(
  indexedDbFactory: IDBFactory | undefined = resolveBrowserIndexedDb(),
): MyDayCacheStorage {
  let databasePromise: Promise<IDBDatabase | null> | null = null;

  const openDatabase = (): Promise<IDBDatabase | null> => {
    if (databasePromise) return databasePromise;
    if (!indexedDbFactory) return Promise.resolve(null);
    databasePromise = new Promise((resolve) => {
      try {
        const request = indexedDbFactory.open(DATABASE_NAME, DATABASE_VERSION);
        request.onupgradeneeded = () => {
          const opened = request.result;
          // Versions 1–2 used unscoped, unbounded entries. They are a
          // reconstructible cache, so v3 deliberately drops them instead of
          // risking cross-owner reads or carrying unbounded historical debt.
          for (const storeName of [REPORT_STORE, MONTH_STORE] as const) {
            if (opened.objectStoreNames.contains(storeName)) {
              opened.deleteObjectStore(storeName);
            }
            opened.createObjectStore(storeName, { keyPath: 'key' })
              .createIndex(TIMESTAMP_INDEX, 'cachedAt');
          }
        };
        request.onsuccess = () => {
          const opened = request.result;
          opened.onversionchange = () => {
            opened.close();
            databasePromise = null;
          };
          resolve(opened);
        };
        request.onerror = () => {
          resolve(null);
        };
        request.onblocked = () => resolve(null);
      } catch {
        resolve(null);
      }
    });
    return databasePromise;
  };

  const read = async (
    storeName: MyDayCacheStoreName,
    key: string,
  ): Promise<unknown | null> => {
    const database = await openDatabase();
    if (!database) return null;
    return new Promise((resolve) => {
      try {
        const request = database.transaction(storeName, 'readonly')
          .objectStore(storeName).get(key);
        request.onsuccess = () => {
          const cached = record(request.result);
          resolve(cached?.value ?? null);
        };
        request.onerror = () => resolve(null);
      } catch {
        resolve(null);
      }
    });
  };

  const write = async (
    storeName: MyDayCacheStoreName,
    cacheRecord: MyDayCacheRecord,
    maximumEntries: number,
  ): Promise<void> => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      try {
        const transaction = database.transaction(storeName, 'readwrite');
        const store = transaction.objectStore(storeName);
        store.put(cacheRecord);
        const countRequest = store.count();
        countRequest.onsuccess = () => {
          let excess = countRequest.result - maximumEntries;
          if (excess <= 0) return;
          const cursorRequest = store.index(TIMESTAMP_INDEX).openCursor();
          cursorRequest.onsuccess = () => {
            const cursor = cursorRequest.result;
            if (!cursor || excess <= 0) return;
            cursor.delete();
            excess -= 1;
            cursor.continue();
          };
        };
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => resolve();
        transaction.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  };

  return Object.freeze({ read, write });
}

export const MYDAY_CACHE_RESOURCE_BUDGET: MyDayCacheResourceBudget = Object.freeze({
  maximumReports: MYDAY_CACHE_MAX_REPORTS,
  maximumMonths: MYDAY_CACHE_MAX_MONTHS,
  maximumReportBytes: MYDAY_CACHE_MAX_REPORT_BYTES,
  maximumMonthBytes: MYDAY_CACHE_MAX_MONTH_BYTES,
  maximumEstimatedBytes: (
    MYDAY_CACHE_MAX_REPORTS * MYDAY_CACHE_MAX_REPORT_BYTES
    + MYDAY_CACHE_MAX_MONTHS * MYDAY_CACHE_MAX_MONTH_BYTES
  ),
});

export function createMyDayPersistentCache(
  ports: MyDayPersistentCachePorts = {},
): MyDayPersistentCache {
  const storage = ports.storage ?? createIndexedDbMyDayCacheStorage();
  const now = ports.now ?? Date.now;

  const readValue = async (
    ownerId: number,
    key: string,
    store: MyDayCacheStoreName,
    kind: 'report' | 'month',
    valid: boolean,
  ): Promise<Record<string, unknown> | null> => {
    if (!validOwnerId(ownerId) || !valid) return null;
    return record(await storage.read(store, scopedKey(ownerId, kind, key)));
  };

  const writeValue = async (
    ownerId: number,
    key: string,
    value: Record<string, unknown>,
    store: MyDayCacheStoreName,
    kind: 'report' | 'month',
    valid: boolean,
    maximumBytes: number,
    maximumEntries: number,
  ): Promise<void> => {
    if (!validOwnerId(ownerId) || !valid || !record(value)) return;
    const size = serializedBytes(value);
    if (size === null || size > maximumBytes) return;
    await storage.write(store, {
      key: scopedKey(ownerId, kind, key),
      value,
      cachedAt: now(),
    }, maximumEntries);
  };

  return Object.freeze({
    readReport: (ownerId: number, date: string) => (
      readValue(ownerId, date, REPORT_STORE, 'report', validDate(date))
    ) as Promise<MyDayReport | null>,
    writeReport: (ownerId: number, date: string, report: MyDayReport) => (
      writeValue(ownerId, date, report, REPORT_STORE, 'report', validDate(date),
        MYDAY_CACHE_MAX_REPORT_BYTES, MYDAY_CACHE_MAX_REPORTS)
    ),
    readMonth: (ownerId: number, month: string) => (
      readValue(ownerId, month, MONTH_STORE, 'month', validMonth(month))
    ) as Promise<MyDayMonthOverview | null>,
    writeMonth: (
      ownerId: number,
      month: string,
      overview: MyDayMonthOverview,
    ) => writeValue(ownerId, month, overview, MONTH_STORE, 'month',
      validMonth(month), MYDAY_CACHE_MAX_MONTH_BYTES, MYDAY_CACHE_MAX_MONTHS),
  });
}

export function myDayLocalDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function myDayDigestFromReport(report: MyDayReport): MyDayDigest {
  const streams = Array.isArray(report.streams) ? report.streams : [];
  const todos = Array.isArray(report.today_todos) ? report.today_todos : [];
  const stats = record(report.stats);
  const rawConversationCount = stats?.totalConversations;
  const convCount = typeof rawConversationCount === 'number'
    && Number.isFinite(rawConversationCount) && rawConversationCount >= 0
    ? rawConversationCount : 0;
  return {
    streams: {
      total: streams.length,
      done: streams.filter((stream) => stream?.status === 'done').length,
      blocked: streams.filter((stream) => stream?.status === 'blocked').length,
    },
    todos: {
      total: todos.length,
      done: todos.filter((todo) => todo?.done === true).length,
    },
    convCount,
  };
}

export function createMyDayReportRepository(
  ports: MyDayReportRepositoryPorts,
): MyDayReportRepository {
  const today = ports.today ?? (() => myDayLocalDateKey(new Date()));

  const publishReport = (date: string, report: MyDayReport): void => {
    if (!validDate(date) || date !== today() || !record(report)) return;
    try { ports.publishDigest?.(myDayDigestFromReport(report)); } catch { /* decorative */ }
  };

  const readReport = (date: string): Promise<MyDayReport | null> => {
    const ownerId = ports.ownerId();
    return ownerId === null
      ? Promise.resolve(null)
      : ports.cache.readReport(ownerId, date);
  };

  const storeReport = (date: string, report: MyDayReport): Promise<void> => {
    publishReport(date, report);
    const ownerId = ports.ownerId();
    return ownerId === null
      ? Promise.resolve()
      : ports.cache.writeReport(ownerId, date, report);
  };

  const readMonth = (month: string): Promise<MyDayMonthOverview | null> => {
    const ownerId = ports.ownerId();
    return ownerId === null
      ? Promise.resolve(null)
      : ports.cache.readMonth(ownerId, month);
  };

  const storeMonth = (
    month: string,
    overview: MyDayMonthOverview,
  ): Promise<void> => {
    const ownerId = ports.ownerId();
    return ownerId === null
      ? Promise.resolve()
      : ports.cache.writeMonth(ownerId, month, overview);
  };

  return Object.freeze({
    readReport,
    storeReport,
    publishReport,
    readMonth,
    storeMonth,
  });
}

let browserCache: MyDayPersistentCache | null = null;

export function browserMyDayPersistentCache(): MyDayPersistentCache {
  browserCache ??= createMyDayPersistentCache();
  return browserCache;
}
