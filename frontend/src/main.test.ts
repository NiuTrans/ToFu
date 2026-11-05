// @vitest-environment jsdom
/** The public entry starts both downloads and preserves the shell failure boundary. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ports = vi.hoisted(() => ({
  locale: vi.fn<() => Promise<void>>(),
  application: vi.fn<() => Promise<void>>(),
  start: vi.fn<() => Promise<void>>(),
}));
vi.mock('./i18n', () => ({ ready: ports.locale }));

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((settle) => { resolve = settle; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  ports.locale.mockResolvedValue();
  ports.application.mockResolvedValue();
  ports.start.mockResolvedValue();
  vi.doMock('./application', async () => {
    await ports.application();
    return { startApplication: ports.start };
  });
});
afterEach(() => vi.restoreAllMocks());

describe('startup entry', () => {
  it('downloads localization and application concurrently and starts once both are ready', async () => {
    const locale = deferred();
    const application = deferred();
    ports.locale.mockReturnValue(locale.promise);
    ports.application.mockReturnValue(application.promise);
    await import('./main');
    await vi.waitFor(() => expect(ports.application).toHaveBeenCalledTimes(1));
    expect(ports.locale).toHaveBeenCalledTimes(1);
    expect(ports.start).not.toHaveBeenCalled();
    locale.resolve();
    await Promise.resolve();
    expect(ports.start).not.toHaveBeenCalled();
    application.resolve();
    await vi.waitFor(() => expect(ports.start).toHaveBeenCalledTimes(1));
  });

  it.each(['locale', 'application', 'start'] as const)(
    'reports %s failure through the existing boot error event', async (stage) => {
      const error = new Error(`${stage} failed`);
      ports[stage].mockRejectedValue(error);
      vi.spyOn(console, 'error').mockImplementation(() => {});
      const failure = vi.fn();
      window.addEventListener('tofu:app-failed', failure);
      try {
        await import('./main');
        await vi.waitFor(() => expect(failure).toHaveBeenCalledTimes(1));
        const reported = (failure.mock.calls[0][0] as CustomEvent).detail.error;
        expect(reported === error || reported.cause === error).toBe(true);
        if (stage !== 'start') expect(ports.start).not.toHaveBeenCalled();
      } finally {
        window.removeEventListener('tofu:app-failed', failure);
      }
    },
  );
});
