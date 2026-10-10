import { describe, expect, it } from 'vitest';
import { presentTurnReasoningMapping } from './turn-serving-route';

describe('response-authoring reasoning mapping', () => {
  it('uses actual resolution, not model name or legacy medium mapping', () => {
    for (const actual of ['high', 'max']) {
      expect(presentTurnReasoningMapping({ model: 'kimi-k3', thinkingDepth: 'medium',
        lastRoundUsage: { reasoningResolution: { requested: 'preset',
          requestedValue: 'medium', actualMode: 'exact', actualValue: actual } },
      })).toBe(`Medium → ${actual === 'max' ? 'Max' : 'High'}`);
    }
  });
  it('never invents actual effort for legacy history', () => {
    expect(presentTurnReasoningMapping({model: 'kimi-k3', thinkingDepth: 'medium'}))
      .toBe('Medium (requested)');
  });
  it('excludes discarded and compaction rounds and stale evidence', () => {
    const apiRounds = [
      {kind: 'agent', usage: {_reasoning_resolution: {requestedValue: 'medium', actualMode: 'exact', actualValue: 'high'}}},
      {kind: 'compaction', usage: {_reasoning_resolution: {actualMode: 'exact', actualValue: 'max'}}},
      {kind: 'agent', responseAuthoring: false, usage: {_reasoning_resolution: {actualMode: 'exact', actualValue: 'max'}}},
    ];
    expect(presentTurnReasoningMapping({apiRounds})).toBe('Medium → High');
    expect(presentTurnReasoningMapping({apiRounds, thinkingDepth: 'medium', lastRoundUsage: {round: 4}}))
      .toBe('Medium (requested)');
  });
});

describe('receipt modes', () => {
  it.each([
    ['medium', 'exact', 'medium', 'Medium'],
    ['off', 'off', '', 'Off'],
    ['default', 'default', '', 'Default (requested)'],
    ['default', 'exact', 'high', 'Default → High'],
    ['medium', 'off', '', 'Medium → Off'],
  ])('renders %s / %s from evidence', (requested, actualMode, actualValue, expected) => {
    expect(presentTurnReasoningMapping({lastRoundUsage: {reasoningResolution: {
      requested, requestedValue: requested === 'medium' ? requested : '', actualMode, actualValue,
    }}})).toBe(expected);
  });
});
