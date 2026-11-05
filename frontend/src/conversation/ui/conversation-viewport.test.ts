// @vitest-environment jsdom
/** Reader intent must win over streaming, late layout and nested evidence. */
import { afterEach, expect, it, vi } from 'vitest';
import { createConversationViewportPort } from './conversation-viewport';

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).reverse().forEach(dispose => dispose()); });

/** Delivers like the platform: after layout, before that frame paints. */
class FakeResizeObserver {
  static live: FakeResizeObserver[] = [];
  private readonly targets = new Set<Element>();
  constructor(private readonly callback: ResizeObserverCallback) { FakeResizeObserver.live.push(this); }
  observe(target: Element) { this.targets.add(target); }
  unobserve(target: Element) { this.targets.delete(target); }
  disconnect() { this.targets.clear(); }
  static deliver() {
    for (const observer of FakeResizeObserver.live) {
      if (observer.targets.size) observer.callback([], observer as unknown as ResizeObserver);
    }
  }
}

function fixture(cancel = () => {}) {
  Object.defineProperty(document.defaultView!, 'ResizeObserver', {
    value: FakeResizeObserver, configurable: true, writable: true,
  });
  const viewport = document.createElement('main');
  const root = document.createElement('article');
  viewport.append(root); document.body.append(viewport);
  let height = 1000, top = 800, client = 200;
  const writes: string[] = [];
  Object.defineProperties(viewport, {
    clientHeight: { get: () => client },
    scrollHeight: { get: () => height },
    scrollTop: { get: () => top, set: (value: number) => {
      writes.push(viewport.style.scrollBehavior);
      top = Math.max(0, Math.min(value, height - client));
    } },
  });
  const frames: Array<() => void> = [];
  const port = createConversationViewportPort(viewport, {
    // Deliberately adversarial: an already delivered callback cannot be recalled.
    scheduleAfterLayout: callback => { frames.push(callback); return cancel; },
  });
  cleanups.push(() => {
    port.dispose?.(); viewport.remove();
    FakeResizeObserver.live = [];
    delete (document.defaultView as { ResizeObserver?: unknown }).ResizeObserver;
  });
  return { viewport, root, port, writes, frames,
    grow: () => { height += 200; },
    /* A pane keeps its offset when it shrinks; layout clamps it when it grows. */
    resize: (value: number) => {
      client = value; top = Math.max(0, Math.min(top, height - client));
      FakeResizeObserver.deliver();
    },
    userScroll: (value: number) => { top = value; viewport.dispatchEvent(new Event('scroll')); },
    /* Script read layout inside a task: the platform clamps the offset of a
     * growing pane there and dispatches that scroll before observing the resize. */
    clamp: (value: number) => {
      client = value; top = Math.max(0, Math.min(top, height - client));
      viewport.dispatchEvent(new Event('scroll'));
    },
    /* One layout pass: the transcript reflows shorter (clamping the offset at the
     * old pane height), then chrome above it grows and shrinks the pane, which
     * keeps that offset. The scroll arrives before the resize is observed. */
    reflowAndShrink: (nextHeight: number, nextClient: number) => {
      height = nextHeight; top = Math.max(0, Math.min(top, height - client));
      client = nextClient;
      viewport.dispatchEvent(new Event('scroll'));
    },
    flush: () => { frames.splice(0).forEach(callback => callback()); },
  };
}

it('keeps a followed tail in view when the pane resizes, before that frame paints', () => {
  const f = fixture();
  f.port.followLatest?.(f.root);
  expect(f.viewport.scrollTop).toBe(800);
  // A composer notice rendered after the commit, or a strip mounted above
  // the transcript, shrinks the pane with no further layout write queued.
  f.resize(171);
  expect(f.viewport.scrollTop).toBe(829);
  expect(f.writes.every(value => value === 'auto')).toBe(true);
  // Layout clamps a growing pane; its delayed scroll event is not reader intent.
  f.resize(200);
  f.viewport.dispatchEvent(new Event('scroll'));
  expect(f.port.following).toBe(true);
  expect(f.viewport.scrollTop).toBe(800);
});

it('keeps following when the clamp of a pinned tail scrolls before the resize is observed', () => {
  const f = fixture();
  f.port.followLatest?.(f.root);
  f.resize(171);
  expect(f.viewport.scrollTop).toBe(829);
  // The clamped offset still sits at the physical bottom: geometry, not intent.
  f.clamp(200);
  expect(f.port.following).toBe(true);
  expect(f.viewport.scrollTop).toBe(800);
  f.grow(); f.port.followLatest?.(f.root, { force: false });
  expect(f.viewport.scrollTop).toBe(1000);
  // Leaving the physical bottom remains reader intent.
  f.userScroll(700);
  expect(f.port.following).toBe(false);
});

it('keeps following when a reflow clamp and a shrinking pane land in one frame', () => {
  const f = fixture();
  f.port.followLatest?.(f.root);
  expect(f.viewport.scrollTop).toBe(800);
  // A narrower window reflows the transcript while the strip above the pane grows:
  // the clamped offset lands 20 px above the new bottom, before the resize is observed.
  f.reflowAndShrink(900, 180);
  expect(f.viewport.scrollHeight - f.viewport.scrollTop - f.viewport.clientHeight).toBe(20);
  expect(f.port.following).toBe(true);
  FakeResizeObserver.deliver();
  expect(f.viewport.scrollTop).toBe(720);
  // A scrollbar drag changes no pane geometry, so it is still reader intent.
  f.userScroll(600);
  expect(f.port.following).toBe(false);
});

it('leaves a suspended reader in place when the pane resizes, and stops observing on disposal', () => {
  const f = fixture();
  f.userScroll(500);
  f.resize(171);
  expect(f.viewport.scrollTop).toBe(500);
  f.port.followLatest?.(f.root);
  f.port.dispose?.();
  f.resize(120);
  expect(f.viewport.scrollTop).toBe(829);
});

it('does not rearm follow during a small upward gesture inside the bottom threshold', () => {
  const f = fixture();
  f.port.followLatest?.(f.root);
  f.viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -10 }));
  f.userScroll(790);
  expect(f.port.following).toBe(false);
  f.grow(); f.flush();
  expect(f.viewport.scrollTop).toBe(790);
  f.userScroll(1000);
  expect(f.port.following).toBe(true);
});

it('keeps following while the reader scrolls the context card, not the transcript', () => {
  const f = fixture();
  const card = document.createElement('div');
  card.dataset.conversationPart = 'turn-context-card';
  const fact = document.createElement('span');
  card.append(fact); f.root.append(card);
  f.port.followLatest?.(f.root);
  fact.dispatchEvent(new WheelEvent('wheel', { deltaY: -10, bubbles: true }));
  expect(f.port.following).toBe(true);
  f.grow(); f.flush();
  expect(f.viewport.scrollTop).toBe(1000);
  // At the card's end the wheel moves the transcript; that movement suspends.
  f.userScroll(900);
  expect(f.port.following).toBe(false);
});

it('recognizes the first scrollbar move and keyboard intent before the next paint', () => {
  const f = fixture();
  f.userScroll(500);
  expect(f.port.following).toBe(false);
  f.port.followLatest?.(f.root);
  f.viewport.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true }));
  f.grow(); f.flush();
  expect(f.viewport.scrollTop).toBe(800);
  expect(f.port.following).toBe(false);
});

it('pins and restores instantly while preserving the exact authored scroll style', () => {
  const f = fixture();
  f.viewport.style.setProperty('scroll-behavior', 'smooth', 'important');
  f.port.followLatest?.(f.root); f.grow(); f.flush();
  expect(f.writes.every(value => value === 'auto')).toBe(true);
  expect(f.viewport.scrollTop).toBe(1000);
  expect(f.viewport.style.getPropertyValue('scroll-behavior')).toBe('smooth');
  expect(f.viewport.style.getPropertyPriority('scroll-behavior')).toBe('important');
});

it('fences every late delivery and scroll write after disposal', () => {
  const f = fixture();
  f.port.followLatest?.(f.root); f.port.dispose?.();
  f.grow(); f.flush(); f.port.followLatest?.(f.root);
  expect(f.viewport.scrollTop).toBe(800);
});

it('releases listeners and fences layout even when native cancellation throws', () => {
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
    const f = fixture(() => { throw new Error('host has closed'); });
    f.port.followLatest?.(f.root);
    expect(() => f.port.dispose?.()).not.toThrow();
    f.grow(); f.flush();
    f.viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -10 }));
    expect(f.port.following).toBe(true); // Removed listener did not receive it.
    expect(f.viewport.scrollTop).toBe(800);
    expect(warning).toHaveBeenCalledTimes(1);
  } finally { warning.mockRestore(); }
});

it('falls back to the captured turn geometry when the reader block is retired', () => {
  const f = fixture();
  const turn = document.createElement('section'); turn.dataset.turnId = 'turn';
  const block = document.createElement('p'); block.dataset.blockId = 'block';
  turn.append(block); f.root.append(turn);
  f.viewport.getBoundingClientRect = () => ({ top: 0, bottom: 200 } as DOMRect);
  turn.getBoundingClientRect = () => ({ top: -20, bottom: 180 } as DOMRect);
  block.getBoundingClientRect = () => ({ top: 30, bottom: 90 } as DOMRect);
  f.userScroll(100);
  const snapshot = f.port.capture(f.root);
  expect(snapshot.anchorBlockId).toBe('block');
  block.remove(); f.port.restore(f.root, snapshot);
  expect(f.viewport.scrollTop).toBe(100);
});

function navigationFixture() {
  const f = fixture();
  f.viewport.getBoundingClientRect = () => ({ top: 0, bottom: 200 } as DOMRect);
  const positions = new Map([['older', 200], ['newer', 600]]);
  for (const [id] of positions) {
    const turn = document.createElement('section');
    turn.dataset.turnId = id;
    turn.getBoundingClientRect = () => ({
      top: positions.get(id)! - f.viewport.scrollTop,
      bottom: positions.get(id)! - f.viewport.scrollTop + 100,
    } as DOMRect);
    f.root.append(turn);
  }
  return { ...f, positions };
}

it('aligns a keyed Turn instantly and corrects one late layout through the same owner', () => {
  const f = navigationFixture();
  f.viewport.style.setProperty('scroll-behavior', 'smooth', 'important');
  const onScroll = vi.fn();
  expect(f.port.scrollToTurn?.(f.root, 'older', onScroll)).toBe(true);
  expect(f.viewport.scrollTop).toBe(200);
  expect(f.port.following).toBe(false);
  expect(onScroll).toHaveBeenCalledTimes(1);
  // A delayed native scroll event from our own write is not reader intent.
  f.viewport.dispatchEvent(new Event('scroll'));
  f.positions.set('older', 250); f.flush();
  expect(f.viewport.scrollTop).toBe(250);
  expect(onScroll).toHaveBeenCalledTimes(2);
  expect(f.writes).toEqual(['auto', 'auto']);
  expect(f.viewport.style.getPropertyValue('scroll-behavior')).toBe('smooth');
  expect(f.viewport.style.getPropertyPriority('scroll-behavior')).toBe('important');
});

it('does not redirect or suspend a viewport for an unavailable or foreign target', () => {
  const f = navigationFixture();
  expect(f.port.scrollToTurn?.(f.root, 'missing')).toBe(false);
  expect(f.port.scrollToTurn?.(document.createElement('article'), 'older')).toBe(false);
  expect(f.port.following).toBe(true);
  expect(f.writes).toEqual([]);
  expect(f.frames).toEqual([]);
});

it('supersedes a prior target without letting its already-delivered frame redirect the reader', () => {
  const f = navigationFixture();
  f.port.scrollToTurn?.(f.root, 'older');
  f.port.scrollToTurn?.(f.root, 'newer');
  f.positions.set('older', 250); f.positions.set('newer', 650);
  f.flush();
  expect(f.viewport.scrollTop).toBe(650);
  expect(f.writes).toHaveLength(3);
});

for (const gesture of ['wheel-up', 'wheel-down', 'PageUp', 'PageDown', 'touch', 'scrollbar'] as const) {
  it(`reader ${gesture} intent retires the deferred target correction`, () => {
    const f = navigationFixture();
    f.port.scrollToTurn?.(f.root, 'older');
    if (gesture.startsWith('wheel')) {
      f.viewport.dispatchEvent(new WheelEvent('wheel', {
        deltaY: gesture === 'wheel-up' ? -10 : 10,
      }));
    } else if (gesture === 'touch') {
      const event = new Event('touchstart');
      Object.defineProperty(event, 'touches', { value: { item: () => ({ clientY: 10 }) } });
      f.viewport.dispatchEvent(event);
    } else if (gesture === 'scrollbar') f.userScroll(220);
    else f.viewport.dispatchEvent(new KeyboardEvent('keydown', { key: gesture }));
    const readerTop = f.viewport.scrollTop;
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(readerTop);
    expect(f.writes).toHaveLength(1);
  });
}

for (const end of ['latest', 'dispose', 'detach'] as const) {
  it(`${end} retires a queued target correction`, () => {
    const f = navigationFixture();
    f.port.scrollToTurn?.(f.root, 'older');
    if (end === 'latest') f.port.followLatest?.(f.root);
    else if (end === 'dispose') f.port.dispose?.();
    else f.root.remove();
    const settledTop = f.viewport.scrollTop;
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(settledTop);
  });
}

it('trusts native scroll anchoring and corrects only what the browser left out of place', () => {
  const f = navigationFixture();
  for (let i = 0; i < 6; i += 1) f.grow();
  f.userScroll(100);
  const snapshot = f.port.capture(f.root);
  // Content was prepended above and the browser's anchoring moved the
  // viewport during layout; its own anchor lands 12px from this leaf.
  f.positions.set('older', 1212); f.viewport.scrollTop = 1100; f.writes.splice(0);
  f.port.restore(f.root, snapshot);
  expect(f.writes).toEqual([]);
  expect(f.viewport.scrollTop).toBe(1100);
  // An adjustment that still leaves the reader far away is corrected.
  f.positions.set('older', 1500);
  f.port.restore(f.root, snapshot);
  expect(f.viewport.scrollTop).toBe(1400);
  expect(f.writes).toEqual(['auto']);
});

it('skips a no-op anchor write but still corrects a real content shift instantly', () => {
  const f = navigationFixture();
  f.userScroll(100);
  const snapshot = f.port.capture(f.root);
  f.port.restore(f.root, snapshot);
  expect(f.writes).toEqual([]);
  f.positions.set('older', 225);
  f.port.restore(f.root, snapshot);
  expect(f.viewport.scrollTop).toBe(125);
  expect(f.writes).toEqual(['auto']);
});

for (const hasLogicalTail of [true, false]) {
  it(`a single scrollbar return cancels target correction and follows only a logical tail (${hasLogicalTail})`, () => {
    const f = navigationFixture();
    // A Surface excludes unmounted later content through this same port.
    const port = hasLogicalTail ? f.port : createConversationViewportPort(f.viewport, {
      canFollowLatest: () => false,
      scheduleAfterLayout: callback => { f.frames.push(callback); return () => {}; },
    });
    if (!hasLogicalTail) { f.port.dispose?.(); cleanups.push(() => port.dispose?.()); }
    port.scrollToTurn?.(f.root, 'older');
    f.userScroll(800);
    expect(port.following).toBe(hasLogicalTail);
    f.positions.set('older', 250); f.flush();
    expect(f.viewport.scrollTop).toBe(800);
    if (hasLogicalTail) {
      f.grow(); port.followLatest?.(f.root, { force: false }); f.flush();
      expect(f.viewport.scrollTop).toBe(1000);
    }
  });
}
