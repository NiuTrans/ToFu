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

describe('guarded recorded-effort presentation', () => {
  it.each(['<img src=x onerror=alert(1)>', 'encrypted_content: opaque', 'x'.repeat(1000), '__proto__']) (
    'omits unknown effort text %s instead of promoting it into UI copy', actualValue => {
      expect(presentTurnReasoningMapping({ thinkingDepth: 'medium', lastRoundUsage: {
        reasoningResolution: { actualMode: 'exact', actualValue, requestedValue: actualValue },
      } })).toBe('Medium (requested)');
    });
  it('treats malformed canonical snapshots as explicit absence, never a legacy receipt fallback', () => {
    for (const lastRoundUsage of [null, [], false, 'missing']) {
      expect(presentTurnReasoningMapping({ thinkingDepth: 'medium', lastRoundUsage,
        apiRounds: [{ usage: { _reasoning_resolution: { actualMode: 'exact', actualValue: 'max' } } }],
      })).toBe('Medium (requested)');
    }
  });
  it('rejects older/auxiliary canonical effort without borrowing from the newer round', () => {
    const reasoningResolution = { requestedValue: 'medium', actualMode: 'exact', actualValue: 'high' };
    const apiRounds = [{ round: 3, usage: { _reasoning_resolution: { actualMode: 'exact', actualValue: 'max' } } }];
    expect(presentTurnReasoningMapping({ thinkingDepth: 'medium', apiRounds,
      lastRoundUsage: { round: 2, reasoningResolution } })).toBe('Medium (requested)');
    expect(presentTurnReasoningMapping({ thinkingDepth: 'medium',
      lastRoundUsage: { tag: 'R2-DISCARDED', reasoningResolution } })).toBe('Medium (requested)');
  });
  it('normalizes exact native case/whitespace without inferring default controls', () => {
    expect(presentTurnReasoningMapping({ lastRoundUsage: { reasoningResolution: {
      actualMode: ' EXACT ', actualValue: ' HIGH ', requestedValue: ' low ',
    } } })).toBe('Low → High');
    expect(presentTurnReasoningMapping({ thinkingDepth: 'default', lastRoundUsage: { reasoningResolution: {
      actualMode: 'default', actualValue: 'high',
    } } })).toBe('Default (requested)');
  });
});
