/** Executable repaint contracts: semantic replay, visible changes and bounded work. */
import { describe, expect, it } from 'vitest';
import type { ConversationQueueItemViewModel } from '../presentation/conversation-view-model';
import { sameBlock, samePresentationValue, sameQueueItem } from './surface-presentation-equality';
import type { TextBlockViewModel } from '../presentation/view-model/types';

describe('presentation repaint decisions', () => {
  it('preserves equal snapshot values across object identity and key order', () => {
    expect(samePresentationValue(
      { command: 'build', receipt: { state: 'queued', revisions: [1, 2] } },
      { receipt: { revisions: [1, 2], state: 'queued' }, command: 'build' },
    )).toBe(true);
  });

  it('repaints changed leaves, missing fields and non-contract values', () => {
    expect(samePresentationValue({ exit: 0 }, { exit: 1 })).toBe(false);
    expect(samePresentationValue({ exit: undefined }, {})).toBe(false);
    expect(samePresentationValue(new Date(0), new Date(0))).toBe(false);
    expect(samePresentationValue([undefined], new Array(1))).toBe(false);
  });

  it('bounds shallow arrays before reading primitive leaves', () => {
    const previous = new Array(100_000).fill('same');
    const current = previous.slice();
    Object.defineProperty(previous, 0, { get() { throw new Error('unbounded read'); } });
    expect(samePresentationValue(previous, current)).toBe(false);
    expect(samePresentationValue(previous, previous)).toBe(true);
  });

  it('shares a visit budget across nested branches instead of resetting per branch', () => {
    const previous = Array.from({ length: 100 }, () => new Array(100).fill(0));
    expect(samePresentationValue(previous, structuredClone(previous))).toBe(false);
  });

  it('bounds deep and cyclic values by requesting a repaint', () => {
    const nested = () => {
      let value: unknown = 0;
      for (let index = 0; index < 100; index += 1) value = { value };
      return value;
    };
    const previous: Record<string, unknown> = {};
    const current: Record<string, unknown> = {};
    previous.self = previous;
    current.self = current;
    expect(samePresentationValue(nested(), nested())).toBe(false);
    expect(samePresentationValue(previous, current)).toBe(false);
  });

  it('keeps queued receipt nodes for semantic replay and repaints new evidence', () => {
    const item = (backgroundCommand: Record<string, unknown>) => ({
      queueId: 'queue-command', position: 1, kind: 'real', text: 'Build',
      source: { backgroundCommand },
    }) as unknown as ConversationQueueItemViewModel;
    const previous = item({ commandId: 'command', state: 'queued' });
    expect(sameQueueItem(previous, item({ state: 'queued', commandId: 'command' }))).toBe(true);
    expect(sameQueueItem(previous, item({ commandId: 'command', state: 'delivered' }))).toBe(false);
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(sameQueueItem(previous, item(cyclic))).toBe(false);
  });
});

it('repaints translation attribution changes but preserves unchanged text blocks', () => {
  const block: TextBlockViewModel = { kind: 'text', blockId: 'answer', source: {}, identitySource: 'contract',
    markdown: 'Body', displayMarkdown: 'Body', displayMode: 'original',
    terminal: true, deliverable: true, resumable: false, translationModel: 'gpt-6-luna' };
  expect(sameBlock(block, { ...block, source: {} })).toBe(true);
  expect(sameBlock(block, { ...block, translationModel: 'recorded-retry-model' })).toBe(false);
  expect(sameBlock(block, { ...block, translationModel: undefined })).toBe(false);
  expect(sameBlock(block, { ...block, inputTranslationCompleted: true })).toBe(false);
});
