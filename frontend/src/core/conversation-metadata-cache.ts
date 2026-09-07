/**
 * Owner-scoped, bounded metadata cache for the conversation catalog.
 *
 * Responsibility: keep reconstructible catalog/settings rows isolated by the
 * authenticated owner, enforce total entry and per-row byte ceilings, and own
 * the IndexedDB connection lifecycle. Entry points:
 * `createConversationMetadataCache` and
 * `createIndexedDbConversationMetadataCacheStorage`. Dependencies: injected
 * identity, clock, and IndexedDB capability; no application globals, network,
 * transcript state, or import-time work.
 */

export const CONVERSATION_CACHE_MAX_METADATA_ROWS = 200;
export const CONVERSATION_CACHE_MAX_SIDEBAR_ROWS = 1000;
export const CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES = 128 * 1024;
export const CONVERSATION_CACHE_MAX_SIDEBAR_ROW_BYTES = 32 * 1024;

const DATABASE_NAME = 'tofu_conv_cache';
const LEGACY_DATABASE_NAME = 'chatui_conv_cache';
const DATABASE_VERSION = 6;
const METADATA_STORE = 'conv_meta';
const SIDEBAR_STORE = 'sidebar_meta';
const OWNER_INDEX = 'ownerId';
const CACHED_AT_INDEX = 'cachedAt';
const MAX_ID_CHARACTERS = 256;
const MAX_TITLE_CHARACTERS = 4096;

type CacheStoreName = typeof METADATA_STORE | typeof SIDEBAR_STORE;
type UnknownRecord = Record<string, unknown>;

export interface ConversationMetadataCacheRow {
  readonly id: string;
  readonly title: string;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly cachedAt: number;
  readonly rev: number | null;
  readonly msgCount: number;
  readonly settings: Readonly<UnknownRecord>;
}

export interface OwnerConversationMetadataCacheRow
  extends ConversationMetadataCacheRow {
  readonly ownerId: number;
}

export interface ConversationMetadataCacheStats {
  readonly count: number;
  readonly messageCount: 0;
  readonly available: boolean;
}

export interface ConversationMetadataCacheStorage {
  isAvailable(): boolean;
  listMetadata(ownerId: number): Promise<ConversationMetadataCacheRow[]>;
  listSidebar(ownerId: number): Promise<ConversationMetadataCacheRow[]>;
  replaceSidebar(
    ownerId: number,
    rows: readonly OwnerConversationMetadataCacheRow[],
    maximumTotalRows: number,
  ): Promise<number>;
  putMetadata(
    row: OwnerConversationMetadataCacheRow,
    maximumTotalRows: number,
  ): Promise<void>;
  remove(ownerId: number, conversationId: string): Promise<void>;
  clearOwner(ownerId: number): Promise<void>;
  countMetadata(ownerId: number): Promise<number>;
  close(): void;
}

export interface ConversationMetadataCache {
  isAvailable(): boolean;
  getAllMeta(): Promise<ConversationMetadataCacheRow[]>;
  getSidebarList(): Promise<ConversationMetadataCacheRow[]>;
  putSidebarList(rows: readonly unknown[]): Promise<number>;
  put(conversation: unknown): Promise<void>;
  remove(conversationId: string): Promise<void>;
  clear(): Promise<void>;
  stats(): Promise<ConversationMetadataCacheStats>;
  close(): void;
}

export interface ConversationMetadataCachePorts {
  readonly storage: ConversationMetadataCacheStorage;
  readonly resolveOwnerId: () => number | null | Promise<number | null>;
  readonly now?: () => number;
}

export interface ConversationMetadataCacheResourceBudget {
  readonly maximumMetadataRows: number;
  readonly maximumSidebarRows: number;
  readonly maximumMetadataRowBytes: number;
  readonly maximumSidebarRowBytes: number;
  readonly maximumEstimatedBytes: number;
}

interface StoredConversationMetadataCacheRow
  extends OwnerConversationMetadataCacheRow {
  readonly cacheKey: string;
}

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function validOwnerId(ownerId: number): boolean {
  return Number.isSafeInteger(ownerId) && ownerId > 0;
}

function normalizedId(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const id = value.trim();
  return id.length > 0 && id.length <= MAX_ID_CHARACTERS ? id : null;
}

function title(value: unknown): string {
  const text = typeof value === 'string' && value.length > 0
    ? value : 'Untitled';
  return text.slice(0, MAX_TITLE_CHARACTERS);
}

function timestamp(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function nonNegativeInteger(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

function serializedBytes(value: unknown): number | null {
  try {
    const serialized = JSON.stringify(value);
    if (typeof serialized !== 'string') return null;
    return typeof TextEncoder === 'function'
      ? new TextEncoder().encode(serialized).byteLength
      : serialized.length * 4;
  } catch {
    return null;
  }
}

function withinByteLimit(value: unknown, maximumBytes: number): boolean {
  const size = serializedBytes(value);
  return size !== null && size <= maximumBytes;
}

function cacheKey(ownerId: number, conversationId: string): string {
  return `owner:${ownerId}:conversation:${conversationId}`;
}

function storedRow(
  row: OwnerConversationMetadataCacheRow,
): StoredConversationMetadataCacheRow {
  return { ...row, cacheKey: cacheKey(row.ownerId, row.id) };
}

function publicRow(
  value: unknown,
  ownerId: number,
  maximumBytes: number,
): ConversationMetadataCacheRow | null {
  const candidate = record(value);
  if (!candidate || candidate.ownerId !== ownerId) return null;
  if (!withinByteLimit(candidate, maximumBytes)) return null;
  const id = normalizedId(candidate.id);
  if (!id) return null;
  const settings = record(candidate.settings) ?? {};
  const row: ConversationMetadataCacheRow = {
    id,
    title: title(candidate.title),
    createdAt: timestamp(candidate.createdAt, 0),
    updatedAt: timestamp(candidate.updatedAt, 0),
    cachedAt: timestamp(candidate.cachedAt, 0),
    rev: typeof candidate.rev === 'number' && Number.isFinite(candidate.rev)
      ? candidate.rev : null,
    msgCount: nonNegativeInteger(candidate.msgCount),
    settings,
  };
  return row;
}

function createStore(database: IDBDatabase, storeName: CacheStoreName): void {
  const store = database.createObjectStore(storeName, { keyPath: 'cacheKey' });
  store.createIndex(OWNER_INDEX, OWNER_INDEX, { unique: false });
  store.createIndex(CACHED_AT_INDEX, CACHED_AT_INDEX, { unique: false });
}

export function createIndexedDbConversationMetadataCacheStorage(
  indexedDbFactory: IDBFactory | undefined,
): ConversationMetadataCacheStorage {
  let available = Boolean(indexedDbFactory);
  let disposed = false;
  let legacyCleanupAttempted = false;
  let databasePromise: Promise<IDBDatabase | null> | null = null;

  const openDatabase = (): Promise<IDBDatabase | null> => {
    if (disposed || !available || !indexedDbFactory) return Promise.resolve(null);
    if (databasePromise) return databasePromise;
    if (!legacyCleanupAttempted) {
      legacyCleanupAttempted = true;
      try { indexedDbFactory.deleteDatabase(LEGACY_DATABASE_NAME); } catch { /* cache only */ }
    }
    databasePromise = new Promise((resolve) => {
      try {
        const request = indexedDbFactory.open(DATABASE_NAME, DATABASE_VERSION);
        request.onupgradeneeded = (event) => {
          const database = request.result;
          const oldVersion = (event as IDBVersionChangeEvent).oldVersion;
          // Versions 1–4 used ownerless keys; a short-lived pre-release v5
          // could retain unrelated legacy stores. This data is reconstructible,
          // so v6 rebuilds the database rather than carrying either shape.
          if (oldVersion < DATABASE_VERSION) {
            for (const storeName of Array.from(database.objectStoreNames)) {
              database.deleteObjectStore(storeName);
            }
            createStore(database, METADATA_STORE);
            createStore(database, SIDEBAR_STORE);
          }
        };
        request.onsuccess = () => {
          const database = request.result;
          if (disposed || !available) {
            database.close();
            resolve(null);
            return;
          }
          database.onversionchange = () => {
            database.close();
            databasePromise = null;
          };
          resolve(database);
        };
        request.onerror = () => {
          available = false;
          resolve(null);
        };
        request.onblocked = () => {
          available = false;
          resolve(null);
        };
      } catch {
        available = false;
        resolve(null);
      }
    });
    return databasePromise;
  };

  const listRows = async (
    storeName: CacheStoreName,
    ownerId: number,
    maximumBytes: number,
  ): Promise<ConversationMetadataCacheRow[]> => {
    const database = await openDatabase();
    if (!database) return [];
    return new Promise((resolve) => {
      const rows: ConversationMetadataCacheRow[] = [];
      try {
        const transaction = database.transaction(storeName, 'readonly');
        const request = transaction.objectStore(storeName)
          .index(OWNER_INDEX).openCursor();
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) return;
          const row = publicRow(cursor.value, ownerId, maximumBytes);
          if (row) rows.push(row);
          cursor.continue();
        };
        transaction.oncomplete = () => resolve(rows);
        transaction.onerror = () => resolve([]);
        transaction.onabort = () => resolve([]);
      } catch {
        resolve([]);
      }
    });
  };

  const evictToLimit = (store: IDBObjectStore, maximumRows: number): void => {
    const countRequest = store.count();
    countRequest.onsuccess = () => {
      let excess = countRequest.result - maximumRows;
      if (excess <= 0) return;
      const cursorRequest = store.index(CACHED_AT_INDEX).openCursor();
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor || excess <= 0) return;
        cursor.delete();
        excess -= 1;
        cursor.continue();
      };
    };
  };

  const deleteOwnerRows = (store: IDBObjectStore, ownerId: number): void => {
    const request = store.index(OWNER_INDEX).openCursor();
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) return;
      if (record(cursor.value)?.ownerId === ownerId) cursor.delete();
      cursor.continue();
    };
  };

  const replaceSidebar = async (
    ownerId: number,
    rows: readonly OwnerConversationMetadataCacheRow[],
    maximumTotalRows: number,
  ): Promise<number> => {
    const database = await openDatabase();
    if (!database) return 0;
    return new Promise((resolve) => {
      try {
        const transaction = database.transaction(SIDEBAR_STORE, 'readwrite');
        const store = transaction.objectStore(SIDEBAR_STORE);
        const cursorRequest = store.index(OWNER_INDEX).openCursor();
        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (cursor) {
            if (record(cursor.value)?.ownerId === ownerId) cursor.delete();
            cursor.continue();
            return;
          }
          for (const row of rows) store.put(storedRow(row));
          evictToLimit(store, maximumTotalRows);
        };
        transaction.oncomplete = () => resolve(rows.length);
        transaction.onerror = () => resolve(0);
        transaction.onabort = () => resolve(0);
      } catch {
        resolve(0);
      }
    });
  };

  const putMetadata = async (
    row: OwnerConversationMetadataCacheRow,
    maximumTotalRows: number,
  ): Promise<void> => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      try {
        const transaction = database.transaction(METADATA_STORE, 'readwrite');
        const store = transaction.objectStore(METADATA_STORE);
        store.put(storedRow(row));
        evictToLimit(store, maximumTotalRows);
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => resolve();
        transaction.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  };

  const remove = async (ownerId: number, conversationId: string): Promise<void> => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      try {
        const transaction = database.transaction(
          [METADATA_STORE, SIDEBAR_STORE], 'readwrite',
        );
        const key = cacheKey(ownerId, conversationId);
        transaction.objectStore(METADATA_STORE).delete(key);
        transaction.objectStore(SIDEBAR_STORE).delete(key);
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => resolve();
        transaction.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  };

  const clearOwner = async (ownerId: number): Promise<void> => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      try {
        const transaction = database.transaction(
          [METADATA_STORE, SIDEBAR_STORE], 'readwrite',
        );
        deleteOwnerRows(transaction.objectStore(METADATA_STORE), ownerId);
        deleteOwnerRows(transaction.objectStore(SIDEBAR_STORE), ownerId);
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => resolve();
        transaction.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  };

  const close = (): void => {
    disposed = true;
    available = false;
    void databasePromise?.then((database) => database?.close());
    databasePromise = null;
  };

  return Object.freeze({
    isAvailable: () => available && !disposed,
    listMetadata: (ownerId: number) => listRows(
      METADATA_STORE, ownerId, CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES,
    ),
    listSidebar: (ownerId: number) => listRows(
      SIDEBAR_STORE, ownerId, CONVERSATION_CACHE_MAX_SIDEBAR_ROW_BYTES,
    ),
    replaceSidebar,
    putMetadata,
    remove,
    clearOwner,
    countMetadata: async (ownerId: number) => (
      await listRows(
        METADATA_STORE, ownerId, CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES,
      )
    ).length,
    close,
  });
}

export const CONVERSATION_METADATA_CACHE_RESOURCE_BUDGET:
ConversationMetadataCacheResourceBudget = Object.freeze({
  maximumMetadataRows: CONVERSATION_CACHE_MAX_METADATA_ROWS,
  maximumSidebarRows: CONVERSATION_CACHE_MAX_SIDEBAR_ROWS,
  maximumMetadataRowBytes: CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES,
  maximumSidebarRowBytes: CONVERSATION_CACHE_MAX_SIDEBAR_ROW_BYTES,
  maximumEstimatedBytes: (
    CONVERSATION_CACHE_MAX_METADATA_ROWS
      * CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES
    + CONVERSATION_CACHE_MAX_SIDEBAR_ROWS
      * CONVERSATION_CACHE_MAX_SIDEBAR_ROW_BYTES
  ),
});

export function extractConversationCacheSettings(
  conversation: unknown,
): Readonly<UnknownRecord> {
  const source = record(conversation) ?? {};
  return {
    model: source.model || source.preset || source.effort,
    provider_id: source.provider_id,
    thinkingDepth: source.thinkingDepth,
    searchMode: source.searchMode,
    fetchEnabled: source.fetchEnabled,
    codeExecEnabled: source.codeExecEnabled,
    browserEnabled: source.browserEnabled,
    desktopEnabled: source.desktopEnabled,
    memoryEnabled: source.memoryEnabled,
    schedulerEnabled: source.schedulerEnabled,
    autopilotEnabled: source.autopilotEnabled,
    activeFlow: source.activeFlow,
    imageGenMode: source.imageGenMode,
    imageGenModel: source.imageGenModel,
    imageGenProviderId: source.imageGenProviderId,
    imageGenCount: source.imageGenCount,
    imageGenAspect: source.imageGenAspect,
    imageGenResolution: source.imageGenResolution,
    humanGuidanceEnabled: source.humanGuidanceEnabled,
    planMode: source.planMode,
    projectPath: source.projectPath,
    projectPaths: source.projectPaths,
    readOnlyPaths: source.readOnlyPaths,
    autoTranslate: source.autoTranslate,
    pinned: source.pinned,
    pinnedAt: source.pinnedAt,
    folderId: source.folderId,
    autopilotSummaries: source.autopilotSummaries,
  };
}

function metadataRow(
  ownerId: number,
  conversation: unknown,
  now: number,
): OwnerConversationMetadataCacheRow | null {
  const source = record(conversation);
  const id = normalizedId(source?.id);
  if (!source || !id) return null;
  const base: OwnerConversationMetadataCacheRow = {
    ownerId,
    id,
    title: title(source.title),
    createdAt: timestamp(source.createdAt, 0),
    updatedAt: timestamp(source.updatedAt ?? source.createdAt, now),
    cachedAt: now,
    rev: typeof source.rev === 'number' && Number.isFinite(source.rev)
      ? source.rev : null,
    msgCount: nonNegativeInteger(source._serverTurnCount),
    settings: extractConversationCacheSettings(source),
  };
  if (withinByteLimit(
    storedRow(base), CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES,
  )) return base;
  const compactSettings = { ...base.settings };
  delete compactSettings.autopilotSummaries;
  const compact = { ...base, settings: compactSettings };
  return withinByteLimit(
    storedRow(compact), CONVERSATION_CACHE_MAX_METADATA_ROW_BYTES,
  )
    ? compact : null;
}

function sidebarRow(
  ownerId: number,
  value: unknown,
  now: number,
): OwnerConversationMetadataCacheRow | null {
  const source = record(value);
  const id = normalizedId(source?.id);
  if (!source || !id) return null;
  const count = source.messageCount ?? source.msgCount ?? source.msg_count;
  const row: OwnerConversationMetadataCacheRow = {
    ownerId,
    id,
    title: title(source.title),
    createdAt: timestamp(source.createdAt ?? source.created_at, 0),
    updatedAt: timestamp(
      source.updatedAt ?? source.updated_at ?? source.createdAt,
      0,
    ),
    cachedAt: now,
    rev: typeof source.rev === 'number' && Number.isFinite(source.rev)
      ? source.rev : null,
    msgCount: nonNegativeInteger(count),
    settings: record(source.settings) ?? {},
  };
  return withinByteLimit(
    storedRow(row), CONVERSATION_CACHE_MAX_SIDEBAR_ROW_BYTES,
  )
    ? row : null;
}

export function createConversationMetadataCache(
  ports: ConversationMetadataCachePorts,
): ConversationMetadataCache {
  const now = ports.now ?? Date.now;

  const ownerId = async (): Promise<number | null> => {
    try {
      const resolved = await ports.resolveOwnerId();
      return resolved !== null && validOwnerId(resolved) ? resolved : null;
    } catch {
      return null;
    }
  };

  const withOwnerRows = async (
    read: (resolvedOwnerId: number) => Promise<ConversationMetadataCacheRow[]>,
  ): Promise<ConversationMetadataCacheRow[]> => {
    const resolvedOwnerId = await ownerId();
    return resolvedOwnerId === null ? [] : read(resolvedOwnerId);
  };

  const putSidebarList = async (values: readonly unknown[]): Promise<number> => {
    const resolvedOwnerId = await ownerId();
    if (resolvedOwnerId === null || !Array.isArray(values)) return 0;
    const observedAt = now();
    const byId = new Map<string, OwnerConversationMetadataCacheRow>();
    for (const value of values) {
      const row = sidebarRow(resolvedOwnerId, value, observedAt);
      if (row) byId.set(row.id, row);
    }
    const rows = [...byId.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, CONVERSATION_CACHE_MAX_SIDEBAR_ROWS);
    return ports.storage.replaceSidebar(
      resolvedOwnerId, rows, CONVERSATION_CACHE_MAX_SIDEBAR_ROWS,
    );
  };

  const put = async (conversation: unknown): Promise<void> => {
    const resolvedOwnerId = await ownerId();
    if (resolvedOwnerId === null) return;
    const row = metadataRow(resolvedOwnerId, conversation, now());
    if (!row) return;
    await ports.storage.putMetadata(
      row, CONVERSATION_CACHE_MAX_METADATA_ROWS,
    );
  };

  const remove = async (conversationId: string): Promise<void> => {
    const resolvedOwnerId = await ownerId();
    const id = normalizedId(conversationId);
    if (resolvedOwnerId === null || !id) return;
    await ports.storage.remove(resolvedOwnerId, id);
  };

  const clear = async (): Promise<void> => {
    const resolvedOwnerId = await ownerId();
    if (resolvedOwnerId !== null) await ports.storage.clearOwner(resolvedOwnerId);
  };

  const stats = async (): Promise<ConversationMetadataCacheStats> => {
    const resolvedOwnerId = await ownerId();
    if (resolvedOwnerId === null || !ports.storage.isAvailable()) {
      return { count: 0, messageCount: 0, available: false };
    }
    return {
      count: await ports.storage.countMetadata(resolvedOwnerId),
      messageCount: 0,
      available: true,
    };
  };

  return Object.freeze({
    isAvailable: () => ports.storage.isAvailable(),
    getAllMeta: () => withOwnerRows(ports.storage.listMetadata),
    getSidebarList: () => withOwnerRows(ports.storage.listSidebar),
    putSidebarList,
    put,
    remove,
    clear,
    stats,
    close: ports.storage.close,
  });
}
