import { describe, expect, it, vi } from 'vitest';
import { createConversationCatalogLoader, type ConversationCatalogResponse } from './conversation-catalog-loader';
import { createConversationCatalogAvailability } from './conversation-catalog-availability';

function response(status: number, items: unknown[] = []): ConversationCatalogResponse {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: name => name === 'X-Total-Count' ? String(items.length) : null },
    json: async () => ({ items }),
  };
}

describe('catalog availability after boot retry exhaustion', () => {
  it('clears the warning when a later background catalog load succeeds', async () => {
    const show = vi.fn(), clear = vi.fn();
    const availability = createConversationCatalogAvailability({ show, clear });
    const requestCatalog = vi.fn()
      .mockResolvedValueOnce(response(500))
      .mockResolvedValueOnce(response(200, [{ id: 'conversation' }]));
    const loader = createConversationCatalogLoader({
      requestCatalog,
      applyAuthoritativeRows: rows => rows.map(row => (row as { id: string }).id),
      hasEveryAppliedRow: () => true,
      writeCache: async () => undefined,
      onSettled: succeeded => availability.settled(succeeded),
      warn: () => {},
    });

    availability.retryStarted();
    await loader.load();
    expect(show).toHaveBeenCalledTimes(1);
    expect(clear).not.toHaveBeenCalled();

    await loader.load();
    expect(loader.serverLoadOk()).toBe(true);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it('clears the warning after a valid not-modified refresh', async () => {
    const clear = vi.fn();
    const requestCatalog = vi.fn()
      .mockResolvedValueOnce(response(200, [{ id: 'conversation' }]))
      .mockResolvedValueOnce(response(304));
    const loader = createConversationCatalogLoader({
      requestCatalog,
      applyAuthoritativeRows: rows => rows.map(row => (row as { id: string }).id),
      hasEveryAppliedRow: () => true,
      writeCache: async () => undefined,
      onSettled: succeeded => { if (succeeded) clear(); },
      warn: () => {},
    });
    await loader.load();
    await loader.load();
    expect(clear).toHaveBeenCalledTimes(2);
  });
});
