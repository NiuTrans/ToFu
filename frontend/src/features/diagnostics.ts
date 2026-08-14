import { getRuntimeService } from '../runtime/app-runtime.js';

export interface FrontendDiagnostics {
  loadedAt: number;
  resourceEntries: number;
  heapBytes?: number;
}

interface LegacyConversation {
  id?: string;
  _needsLoad?: boolean;
  messages?: unknown[];
  _serverMsgCount?: number;
  _windowed?: boolean;
  _trimmed?: boolean;
  _hasMoreEarlier?: boolean;
  _totalCount?: number | null;
}

interface DiagnosticResponse {
  status: number;
  text(): Promise<string>;
}

interface DiagnosticApi {
  conversations?: {
    getResponse(
      id: string,
      options: Record<string, unknown>,
    ): Promise<DiagnosticResponse>;
  };
}

type JsonObject = Record<string, unknown>;

function activeConversationId(): string | null {
  const value = getRuntimeService('activeConvId');
  return typeof value === 'string' ? value : null;
}

function conversationList(): LegacyConversation[] {
  const value = getRuntimeService('conversations');
  return Array.isArray(value) ? value as LegacyConversation[] : [];
}

function conversationWindowParam(): string {
  const value = getRuntimeService('convWindowParam');
  return typeof value === 'function' ? String(value()) : '';
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

function activeConversationSnapshot(): JsonObject {
  return safe<JsonObject>(() => {
    const id = activeConversationId();
    const conversations = conversationList();
    if (!id) return { activeConvId: id };
    const conversation = conversations.find((item) => item?.id === id);
    if (!conversation) return { activeConvId: id, found: false };
    return {
      activeConvId: id,
      found: true,
      needsLoad: Boolean(conversation._needsLoad),
      inMemoryMsgCount: conversation.messages?.length ?? 0,
      serverMsgCount: conversation._serverMsgCount ?? 0,
      windowed: Boolean(conversation._windowed),
      trimmed: Boolean(conversation._trimmed),
      hasMoreEarlier: Boolean(conversation._hasMoreEarlier),
      totalCount: conversation._totalCount ?? null,
    };
  }, { error: 'activeConv snapshot failed' });
}

function skeletonShowing(): boolean | null {
  return safe(() => {
    const inner = document.getElementById('chatInner');
    if (!inner) return null;
    const text = inner.textContent ?? '';
    return text.includes('Fetching') && text.includes('from server');
  }, null);
}

function windowConfiguration(): JsonObject {
  return safe<JsonObject>(() => ({
    windowParam: conversationWindowParam() || '(fn missing)',
    override: getRuntimeService('TOFU_CONV_WINDOW') ?? null,
  }), { error: 'window config failed' });
}

function elapsedSince(startedAt: number): number {
  return Math.round(safe(() => performance.now(), Date.now()) - startedAt);
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

async function liveGetProbe(): Promise<JsonObject> {
  const id = safe(
    activeConversationId,
    null,
  );
  if (!id) return { skipped: 'no active conversation' };

  const param = safe(
    conversationWindowParam,
    '',
  );
  const requestedWith = param ? `window=${param}` : '(no window param — full blob)';
  const startedAt = safe(() => performance.now(), Date.now());
  const controller = safe(() => new AbortController(), null);
  const timer = window.setTimeout(() => safe(() => controller?.abort(), undefined), 15_000);
  const api = window.Api as unknown as DiagnosticApi | undefined;

  if (typeof api?.conversations?.getResponse !== 'function') {
    window.clearTimeout(timer);
    return { requestedWith, skipped: 'Api client unavailable' };
  }

  const options: Record<string, unknown> = {
    headers: { Accept: 'application/json' },
    onError: 'throw',
    timeout: 0,
  };
  if (param) options.query = { window: param };
  if (controller) options.signal = controller.signal;

  try {
    const response = await api.conversations.getResponse(id, options);
    const body = await response.text();
    let windowedFlag: boolean | null = null;
    let totalCount: unknown = null;
    let messagesReturned: number | null = null;
    let jsonParseError: string | null = null;
    try {
      const parsed = JSON.parse(body) as JsonObject | null;
      windowedFlag = parsed?.windowed === true;
      totalCount = parsed?.totalCount ?? null;
      messagesReturned = Array.isArray(parsed?.messages) ? parsed.messages.length : 0;
    } catch (error) {
      jsonParseError = errorMessage(error);
    }
    return {
      requestedWith,
      httpStatus: response.status,
      bodyBytes: body.length,
      elapsedMs: elapsedSince(startedAt),
      serverSaysWindowed: windowedFlag,
      totalCount,
      messagesReturned,
      jsonParseError,
    };
  } catch (error) {
    const aborted = safe(
      () => (error as { name?: string } | null)?.name === 'AbortError',
      false,
    );
    return {
      requestedWith,
      failed: true,
      aborted,
      note: aborted
        ? 'fetch aborted at 15s — body never fully arrived (tunnel buffering / truncation suspected)'
        : `fetch error: ${errorMessage(error)}`,
      elapsedMs: elapsedSince(startedAt),
    };
  } finally {
    window.clearTimeout(timer);
  }
}

function serializeDiagnostics(blob: JsonObject): string {
  try {
    return JSON.stringify(blob, null, 2);
  } catch (error) {
    return JSON.stringify({
      collectedAt: new Date().toISOString(),
      error: `diagnostics serialization failed: ${errorMessage(error)}`,
    });
  }
}

/**
 * Collect the Android/Web diagnostics contract without trusting application
 * state to be healthy. This function resolves to JSON even when every optional
 * legacy global is absent or the live probe fails.
 */
export async function collectDiagnostics(): Promise<string> {
  try {
    const blob: JsonObject = {
      collectedAt: new Date().toISOString(),
      note: 'Tofu client diagnostics — paste this to the maintainer.',
      location: safe(() => location.href, null),
      userAgent: safe(() => navigator.userAgent, null),
      viewport: safe(() => ({
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        dpr: window.devicePixelRatio,
        vh100: document.documentElement.style.getPropertyValue('--vh100') || '(unset)',
      }), null),
      bundle: safe(() => {
        const script = document.querySelector<HTMLScriptElement>('script[src*="bundle-"]');
        return script
          ? (script.getAttribute('src') ?? '').replace(/^.*\//, '')
          : '(dev, unbundled)';
      }, null),
      conversationCount: safe(
        () => conversationList().length,
        null,
      ),
      windowConfig: windowConfiguration(),
      skeletonShowing: skeletonShowing(),
      activeConv: activeConversationSnapshot(),
      recentLog: safe(() => {
        const ring = getRuntimeService('__tofuDiagRing');
        return Array.isArray(ring) ? ring.slice(-60) : [];
      }, []),
    };
    blob.liveGetProbe = await liveGetProbe();
    return serializeDiagnostics(blob);
  } catch (error) {
    return serializeDiagnostics({
      collectedAt: new Date().toISOString(),
      error: `diagnostics collection failed: ${errorMessage(error)}`,
    });
  }
}

export function snapshotDiagnostics(): FrontendDiagnostics {
  const memory = (performance as Performance & {
    memory?: { usedJSHeapSize?: number };
  }).memory;
  return {
    loadedAt: Date.now(),
    resourceEntries: performance.getEntriesByType('resource').length,
    ...(memory?.usedJSHeapSize ? { heapBytes: memory.usedJSHeapSize } : {}),
  };
}
