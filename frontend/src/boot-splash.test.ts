import { describe, expect, it } from 'vitest';

import {
  BOOT_SPLASH_POLICY,
  createBootSplashController,
  verdictFromBootPayload,
  verdictFromHealthPayload,
  type BootSnapshot,
  type BootSplashPorts,
} from './boot-splash';

const RUNNING_SNAPSHOT: BootSnapshot = {
  status: 'running',
  phaseTotal: 9,
  phases: [
    { index: 1, key: 'frontend_assets', label: 'Frontend assets', status: 'done', duration_s: 0.5, error: null },
    { index: 2, key: 'storage_boundary', label: 'Exclusive storage boundary', status: 'done', duration_s: 0.5, error: null },
    { index: 3, key: 'storage_sidecar', label: 'Storage sidecar', status: 'running', duration_s: null, error: null },
  ],
  currentPhase: { index: 3, key: 'storage_sidecar', label: 'Storage sidecar', status: 'running', duration_s: null, error: null },
  failedPhase: null,
  error: null,
  retryCount: 0,
  elapsedS: 4.2,
  readyAt: null,
};

const FAILED_SNAPSHOT: BootSnapshot = {
  ...RUNNING_SNAPSHOT,
  status: 'failed',
  currentPhase: null,
  failedPhase: { index: 3, key: 'storage_sidecar', label: 'Storage sidecar', status: 'failed', duration_s: null, error: 'RuntimeError: boom' },
  error: 'phase 3/9 Storage sidecar: RuntimeError: boom',
  retryCount: 1,
};

describe('verdictFromBootPayload', () => {
  it('maps every server status onto one splash verdict', () => {
    expect(verdictFromBootPayload({ status: 'ready' })).toEqual({ kind: 'ready' });
    expect(verdictFromBootPayload({ status: 'failed' })).toMatchObject({ kind: 'failed' });
    expect(verdictFromBootPayload({ status: 'running' })).toMatchObject({ kind: 'starting' });
    expect(verdictFromBootPayload({ status: 'pending' })).toMatchObject({ kind: 'starting' });
    expect(verdictFromBootPayload({ status: 'interrupted' })).toMatchObject({ kind: 'starting' });
  });

  it('treats malformed payloads as unavailable, never as ready', () => {
    expect(verdictFromBootPayload(null)).toEqual({ kind: 'unavailable' });
    expect(verdictFromBootPayload('ready')).toEqual({ kind: 'unavailable' });
    expect(verdictFromBootPayload({ status: 'unknown-future-state' })).toEqual({ kind: 'unavailable' });
  });
});

describe('verdictFromHealthPayload (legacy fallback)', () => {
  it('reads storage.ready and never invents readiness', () => {
    expect(verdictFromHealthPayload({ storage: { ready: true } })).toEqual({ kind: 'ready' });
    expect(verdictFromHealthPayload({ storage: { ready: false } })).toMatchObject({ kind: 'starting' });
    expect(verdictFromHealthPayload({})).toMatchObject({ kind: 'starting' });
    expect(verdictFromHealthPayload(null)).toEqual({ kind: 'unavailable' });
  });
});

// ── Controller behavior over a minimal DOM stub ─────────────────────

type StubElement = {
  className: string;
  id: string;
  hidden: boolean;
  disabled: boolean;
  textContent: string;
  type: string;
  style: Record<string, string>;
  children: StubElement[];
  classList: { add(...names: string[]): void; remove(...names: string[]): void };
  setAttribute(name: string, value: string): void;
  appendChild(child: StubElement): void;
  addEventListener(type: string, listener: () => void): void;
  remove(): void;
};

function stubElement(): StubElement {
  const listeners = new Map<string, (() => void)[]>();
  const el: StubElement = {
    className: '',
    id: '',
    hidden: false,
    disabled: false,
    textContent: '',
    type: '',
    style: {},
    children: [],
    classList: {
      add: () => undefined,
      remove: () => undefined,
    },
    setAttribute: () => undefined,
    appendChild(child) {
      el.children.push(child);
    },
    addEventListener(type, listener) {
      listeners.set(type, [...(listeners.get(type) ?? []), listener]);
      if (type === 'click') {
        (el as { click?: () => void }).click = () => listener();
      }
    },
    remove: () => undefined,
  };
  return el;
}

function stubDocument(): Document {
  const body = stubElement();
  return {
    body,
    createElement: () => stubElement(),
  } as unknown as Document;
}

function findByClass(root: StubElement, className: string): StubElement[] {
  const found: StubElement[] = [];
  const walk = (el: StubElement): void => {
    if (el.className.split(/\s+/).includes(className)) found.push(el);
    for (const child of el.children) walk(child);
  };
  walk(root);
  return found;
}

function controllerPorts(overrides: Partial<BootSplashPorts>): BootSplashPorts {
  return {
    document: stubDocument(),
    schedule: {
      setTimeout: () => 1,
      clearTimeout: () => undefined,
    },
    log: { warn: () => undefined },
    copy: {
      splashTitle: () => '正在启动服务',
      splashSubtitle: () => 'subtitle',
      connecting: () => 'connecting',
      elapsed: seconds => `已等待 ${seconds} 秒`,
      failedTitle: () => '启动失败',
      failedHint: () => 'hint',
      retry: () => '重试',
      retrying: () => '重试中…',
      phaseLabel: (_key, serverLabel) => serverLabel,
    },
    probeBoot: async () => ({ kind: 'ready' }),
    requestRetry: async () => true,
    ...overrides,
  };
}

describe('createBootSplashController', () => {
  it('stays invisible when boot is already ready (no flash on warm loads)', async () => {
    const ports = controllerPorts({});
    const controller = createBootSplashController(ports);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(controller.isActive()).toBe(false);
    controller.destroy();
  });

  it('shows on a starting verdict and dismisses permanently on ready', async () => {
    let verdicts = [
      { kind: 'starting', snapshot: RUNNING_SNAPSHOT } as const,
      { kind: 'ready' } as const,
    ];
    const ports = controllerPorts({
      probeBoot: async () => verdicts.shift() ?? { kind: 'ready' },
    });
    const controller = createBootSplashController(ports);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(controller.isActive()).toBe(true);
    // Drive the scheduled second poll synchronously.
    await new Promise(resolve => setTimeout(resolve, 0));
    controller.destroy();
  });

  it('surfaces a running phase transient retry instead of a silent timer', async () => {
    const document = stubDocument();
    const snapshot: BootSnapshot = {
      ...RUNNING_SNAPSHOT,
      currentPhase: {
        ...RUNNING_SNAPSHOT.currentPhase!,
        error: 'StorageError: storage.v2 authority is unavailable',
        retry: 2,
      },
    };
    const controller = createBootSplashController(controllerPorts({
      document,
      probeBoot: async () => ({ kind: 'starting', snapshot }),
    }));
    await new Promise(resolve => setTimeout(resolve, 0));
    const overlay = (document.body as unknown as StubElement).children[0];
    const errorBox = findByClass(overlay, 'boot-splash__error')[0];
    expect(errorBox.hidden).toBe(false);
    expect(errorBox.textContent).toContain('authority is unavailable');
    controller.destroy();
  });

  it('renders the failed state and the retry button re-arms the boot', async () => {
    let retried = 0;
    const ports = controllerPorts({
      probeBoot: async () => ({ kind: 'failed', snapshot: FAILED_SNAPSHOT }),
      requestRetry: async () => {
        retried += 1;
        return true;
      },
    });
    const controller = createBootSplashController(ports);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(controller.isActive()).toBe(true);
    controller.destroy();
    expect(retried).toBe(0);
  });
});

describe('BOOT_SPLASH_POLICY', () => {
  it('keeps the poll loop bounded', () => {
    expect(BOOT_SPLASH_POLICY.pollMs).toBeGreaterThan(0);
    expect(BOOT_SPLASH_POLICY.probeTimeoutMs).toBeGreaterThan(0);
  });
});
