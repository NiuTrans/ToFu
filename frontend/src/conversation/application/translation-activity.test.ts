import { describe, expect, it } from 'vitest';
import { mergeTranslationActivity } from './translation-activity';

describe('translation preview deltas', () => {
  it('merges independent blocks and replacement chunks without mutating incoming DTOs', () => {
    const source = { 'text:llm-1': 'one' };
    const first = mergeTranslationActivity(undefined, { status: 'pending', partialByRound: source });
    const next = mergeTranslationActivity(first, { status: 'pending', partialByRound: { 'text:llm-2': 'two' } });
    const final = mergeTranslationActivity(next, { status: 'pending', partialByRound: { 'text:llm-1': 'ONE' } });
    expect(final.partialByRound).toEqual({ 'text:llm-1': 'ONE', 'text:llm-2': 'two' });
    expect(final.partial).toBe('ONE\n\ntwo');
    expect(source).toEqual({ 'text:llm-1': 'one' });
    expect(next.partialByRound).toBe(first.partialByRound);
  });
  it('keeps reasoning on its own blocks and sorts narration by numeric round', () => {
    const value = mergeTranslationActivity(undefined, { status: 'pending', partialByRound: {
      'text:llm-10': 'ten', 'thinking:llm-1': 'reason', 'text:llm-2': 'two',
    } });
    expect(value.partial).toBe('two\n\nten');
    expect(value.partialByRound?.['thinking:llm-1']).toBe('reason');
  });
  it('clears explicit empty previews and preserves whole-turn streaming', () => {
    const first = mergeTranslationActivity(undefined, { status: 'pending', partialByRound: { one: 'one' } });
    const reset = mergeTranslationActivity(first, { status: 'pending', partialByRound: {} });
    expect(reset.partialByRound).toEqual({});
    expect(reset.partial).toBe('');
    expect(mergeTranslationActivity(first, { status: 'pending', partial: 'whole turn' }).partial).toBe('whole turn');
    expect(mergeTranslationActivity(first, { status: 'failed', error: 'failed' }).partialByRound).toBeUndefined();
  });
  it('converges on the same preview when superseded cumulative partials are skipped', () => {
    const final = '完整译文。'.repeat(8);
    const frames = Array.from(final, (_char, index) => (
      { status: 'pending' as const, partialByRound: { 'text:llm-1': final.slice(0, index + 1) } }));
    const merge = (sequence: typeof frames) => sequence.reduce(
      (activity, frame) => mergeTranslationActivity(activity, frame),
      mergeTranslationActivity(undefined, { status: 'pending', partialByRound: { 'text:llm-0': 'zero' } }));
    const every = merge(frames);
    const sparse = merge(frames.filter((_frame, index) => index % 7 === 3 || index === frames.length - 1));
    const latestOnly = merge(frames.slice(-1));
    for (const skipped of [sparse, latestOnly]) {
      expect(skipped.partialByRound).toEqual(every.partialByRound);
      expect(skipped.partial).toBe(every.partial);
    }
    expect(every.partialByRound?.['text:llm-1']).toBe(final);
  });
  it('accepts prototype-shaped keys without altering inherited behavior', () => {
    const incoming = JSON.parse('{"__proto__":"preview","constructor":"ctor"}');
    const value = mergeTranslationActivity(undefined, { status: 'pending', partialByRound: incoming });
    expect(Object.getPrototypeOf(value.partialByRound)).toBeNull();
    expect(value.partialByRound?.['__proto__']).toBe('preview');
  });
});
