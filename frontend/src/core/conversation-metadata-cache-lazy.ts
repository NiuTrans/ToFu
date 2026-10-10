/**
 * Lazy composition proxy for the optional conversation metadata cache.
 *
 * Responsibility: keep the IndexedDB implementation out of the first-screen
 * bundle, coalesce its first demand, preserve the synchronous capability
 * probe expected by retained callers, and close a cache that resolves during
 * teardown. Entry point: `createLazyConversationMetadataCache`. Dependencies:
 * injected capability and loader ports; no browser or application globals.
 */

import type {
  ConversationMetadataCache,
  ConversationMetadataCacheRow,
  ConversationMetadataCacheStats,
} from './conversation-metadata-cache';

export interface LazyConversationMetadataCachePorts {
  readonly isCapabilityAvailable: () => boolean;
  readonly load: () => Promise<ConversationMetadataCache>;
}

export function createLazyConversationMetadataCache(
  ports: LazyConversationMetadataCachePorts,
): ConversationMetadataCache {
  let disposed = false;
  let resolvedCache: ConversationMetadataCache | null = null;
  let cachePromise: Promise<ConversationMetadataCache | null> | null = null;

  const resolveCache = (): Promise<ConversationMetadataCache | null> => {
    if (disposed || !ports.isCapabilityAvailable()) return Promise.resolve(null);
    if (resolvedCache) return Promise.resolve(resolvedCache);
    if (cachePromise) return cachePromise;
    cachePromise = ports.load().then((cache) => {
      if (disposed) {
        cache.close();
        return null;
      }
      resolvedCache = cache;
      return cache;
    }).catch(() => null).finally(() => {
      cachePromise = null;
    });
    return cachePromise;
  };

  const rows = async (
    read: (cache: ConversationMetadataCache) => Promise<ConversationMetadataCacheRow[]>,
  ): Promise<ConversationMetadataCacheRow[]> => {
    const cache = await resolveCache();
    return cache ? read(cache) : [];
  };

  const noResult = async (
    write: (cache: ConversationMetadataCache) => Promise<void>,
  ): Promise<void> => {
    const cache = await resolveCache();
    if (cache) await write(cache);
  };

  const close = (): void => {
    if (disposed) return;
    disposed = true;
    resolvedCache?.close();
    resolvedCache = null;
  };

  return Object.freeze({
    isAvailable: () => !disposed && ports.isCapabilityAvailable()
      && (resolvedCache?.isAvailable() ?? true),
    getAllMeta: () => rows((cache) => cache.getAllMeta()),
    getSidebarList: () => rows((cache) => cache.getSidebarList()),
    putSidebarList: async (values: readonly unknown[]) => {
      const cache = await resolveCache();
      return cache ? cache.putSidebarList(values) : 0;
    },
    put: (conversation: unknown) => noResult((cache) => cache.put(conversation)),
    remove: (conversationId: string) => noResult(
      (cache) => cache.remove(conversationId),
    ),
    clear: () => noResult((cache) => cache.clear()),
    stats: async (): Promise<ConversationMetadataCacheStats> => {
      const cache = await resolveCache();
      return cache
        ? cache.stats()
        : { count: 0, messageCount: 0, available: false };
    },
    close,
  });
}
