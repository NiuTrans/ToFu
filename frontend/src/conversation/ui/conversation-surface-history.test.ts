// @vitest-environment jsdom
/** Older-history read-ahead: when the Surface asks for a page, and how a
 * landed page retires its flight. Geometry is stubbed; no transport. */
import { afterEach, beforeEach, expect, it } from 'vitest';
import { createTurnState, type TurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import type { ConversationIntent } from './surface-contracts';
import { createConversationSurface, type ConversationSurface } from './conversation-surface';
import { CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS } from './surface-windowing';

const VIEWPORT = 700;
const TURN = 400;

function laneState(first: number, last: number, nextBeforeOrdinal: number): TurnState {
  const state = createTurnState('conv');
  for (let ordinal = first; ordinal <= last; ordinal += 1) {
    const id = `turn-${ordinal}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main',
      ordinal, actor: ordinal % 2 ? 'assistant' : 'human', kind: ordinal % 2 ? 'reply' : 'input',
      status: 'completed', currentAttemptId: null, projectionRevision: 1, createdAt: ordinal,
      updatedAt: ordinal, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  state.historyByLane.main = { nextBeforeOrdinal, hasMore: nextBeforeOrdinal > 0, totalTurns: 100 };
  return state;
}

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

let viewport: HTMLElement;
let container: HTMLElement;
let frames: FrameRequestCallback[];
let surface: ConversationSurface | null;
let intents: ConversationIntent[];
let pages: Array<ReturnType<typeof deferred>>;
const restore: Array<() => void> = [];

beforeEach(() => {
  viewport = document.createElement('main');
  container = document.createElement('div');
  viewport.append(container); document.body.append(viewport);
  frames = []; intents = []; pages = []; surface = null;
  const view = window;
  const request = view.requestAnimationFrame, cancel = view.cancelAnimationFrame;
  view.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  view.cancelAnimationFrame = () => {};
  const rect = HTMLElement.prototype.getBoundingClientRect;
  // Turns stack 400px apart from the top of the scrolled content.
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    const box = (top: number, height: number, width = 800) => ({ top, bottom: top + height,
      left: 0, right: width, width, height, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
    if (this === viewport) return box(0, VIEWPORT);
    const index = [...container.querySelectorAll('[data-turn-id]')].indexOf(this);
    return index >= 0 ? box(index * TURN - viewport.scrollTop, TURN) : box(0, 0, 0);
  };
  Object.defineProperty(viewport, 'clientHeight', { value: VIEWPORT, configurable: true });
  restore.push(() => {
    view.requestAnimationFrame = request; view.cancelAnimationFrame = cancel;
    HTMLElement.prototype.getBoundingClientRect = rect;
  });
});

afterEach(() => {
  surface?.dispose();
  viewport.remove();
  restore.splice(0).forEach(undo => undo());
});

function mount(state: TurnState): ConversationSurface {
  surface = createConversationSurface(container, {
    scrollViewport: viewport,
    scrollAnchor: {
      following: false,
      capture: () => null, restore: () => {}, suspendFollow: () => {},
    },
    onIntent(intent) {
      intents.push(intent);
      const page = deferred();
      pages.push(page);
      return page.promise;
    },
  });
  surface.render(selectConversationViewModel(state));
  frames.splice(0).forEach(callback => callback(0));
  return surface;
}

/** One reader scroll to `top`, delivered like a native scroll event. */
function scrollTo(top: number): void {
  viewport.scrollTop = top;
  viewport.dispatchEvent(new Event('scroll'));
}

function runFrames(): void {
  for (let guard = 0; frames.length && guard < 20; guard += 1) {
    frames.splice(0).forEach(callback => callback(0));
  }
}

const historyState = (): string | undefined => surface?.root
  .querySelector<HTMLElement>('[data-history-boundary="earlier"]')?.dataset.historyState;

it('requests the next page while several viewports of loaded history remain above the reader', () => {
  mount(laneState(40, 47, 40));
  // The loaded top is 2.5 viewports above: beyond the residency prefetch
  // distance, inside the read-ahead distance.
  const above = VIEWPORT * 2.5;
  expect(above).toBeLessThan(VIEWPORT * CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS);
  scrollTo(above + 10);
  runFrames();
  expect(intents).toHaveLength(0); // Moving toward later content is not demand.
  scrollTo(above);
  runFrames();
  expect(intents).toEqual([expect.objectContaining({
    type: 'load-earlier-turns', laneId: 'main', beforeOrdinal: 40 })]);
  expect(historyState()).toBe('loading');
});

it('never requests history without reader demand toward earlier content', () => {
  mount(laneState(40, 47, 40));
  viewport.scrollTop = 100;
  surface!.render(selectConversationViewModel(laneState(40, 47, 40)));
  runFrames();
  expect(intents).toHaveLength(0);
});

it('keeps a landed page in flight until the Store render that carries it, then reads the next cursor', async () => {
  mount(laneState(40, 47, 40));
  scrollTo(910);
  scrollTo(900);
  runFrames();
  expect(intents.map(intent => intent.beforeOrdinal)).toEqual([40]);
  // A reader frame queued before the response, the Store paint scheduled when
  // the coordinator publishes the page, then the request promise settles.
  scrollTo(850);
  frames.push(() => surface!.render(selectConversationViewModel(laneState(32, 47, 32))));
  pages[0].resolve();
  await Promise.resolve(); await Promise.resolve();
  expect(historyState()).toBe('loading');
  const queued = frames.splice(0);
  queued[0](0);
  // The reader frame ran against the previous graph: no second read of 40.
  expect(intents.map(intent => intent.beforeOrdinal)).toEqual([40]);
  queued.slice(1).forEach(callback => callback(0));
  runFrames();
  expect(historyState()).toBe('idle');
  expect(surface!.root.querySelector('[data-turn-id="turn-39"]')).not.toBeNull();
  scrollTo(800);
  runFrames();
  expect(intents.map(intent => intent.beforeOrdinal)).toEqual([40, 32]);
});

it('retries a failed page only when the reader returns near the loaded top', async () => {
  mount(laneState(40, 47, 40));
  scrollTo(VIEWPORT * 2.5 + 10);
  scrollTo(VIEWPORT * 2.5);
  runFrames();
  expect(intents).toHaveLength(1);
  pages[0].reject(new Error('history unavailable'));
  await Promise.resolve(); await Promise.resolve();
  expect(historyState()).toBe('failed');
  scrollTo(VIEWPORT * 2.4);
  runFrames();
  expect(intents).toHaveLength(1); // Read-ahead distance alone never retries.
  scrollTo(VIEWPORT * 0.5);
  runFrames();
  expect(intents.map(intent => intent.beforeOrdinal)).toEqual([40, 40]);
  expect(historyState()).toBe('loading');
});

it('retires a settled flight on the next frame when no Store render consumes it', async () => {
  mount(laneState(40, 47, 40));
  scrollTo(910);
  scrollTo(900);
  runFrames();
  expect(intents).toHaveLength(1);
  pages[0].resolve();
  await Promise.resolve(); await Promise.resolve();
  expect(historyState()).toBe('loading');
  runFrames();
  expect(historyState()).toBe('idle');
});
