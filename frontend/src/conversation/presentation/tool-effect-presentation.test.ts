import { describe, expect, it } from 'vitest';
import { toolEffectNotice } from './tool-effect-presentation';
import type { ToolExecutionEffect, TurnStatus } from '../../api/conversation-sync.generated';

const effect: ToolExecutionEffect = { operationId: 'a'.repeat(32), state: 'entered', recovery: 'reconcile', scope: 'attempt' };
describe('tool effect uncertainty', () => {
  it.each<TurnStatus>(['completed', 'failed', 'interrupted', 'truncated'])('retains uncertainty after %s', status => {
    expect(toolEffectNotice(effect, status)).toBe('reconcile');
  });
  it('never treats a live entered call or a returned handler response as unknown', () => {
    expect(toolEffectNotice(effect, 'running')).toBeNull();
    expect(toolEffectNotice(effect, 'pending')).toBeNull();
    expect(toolEffectNotice({ ...effect, state: 'returned' }, 'completed')).toBeNull();
    expect(toolEffectNotice({ ...effect, state: 'not_started' }, 'failed')).toBeNull();
    expect(toolEffectNotice(undefined, 'interrupted')).toBeNull();
  });
  it('keeps a lost reply visible while the agent continues and distinguishes idempotency', () => {
    expect(toolEffectNotice({ ...effect, state: 'unknown' }, 'running')).toBe('reconcile');
    expect(toolEffectNotice({ ...effect, state: 'unknown', recovery: 'idempotent' }, 'running')).toBe('idempotent');
    expect(toolEffectNotice({ ...effect, state: 'unknown', recovery: 'read_only' }, 'failed')).toBeNull();
  });
});

import { selectTurnBlocks } from './view-model/turn-blocks';
import { sameBlock } from '../ui/surface-presentation-equality';
import type { TurnRecord } from '../../api/conversation-sync.generated';

it('repaints unchanged tool evidence on terminal transition without mutating the source', () => {
  const round = { toolName: 'perform', roundNum: 1, executionEffect: effect };
  const live = { turnId: 'turn', actor: 'assistant', status: 'running', projection: { segments: [
    { type: 'tool_use', blockId: 'call', id: 'call', name: 'perform', input: {}, _round: round },
  ] } } as unknown as TurnRecord;
  const before = JSON.stringify(live);
  const initial = selectTurnBlocks(live)[0];
  const final = selectTurnBlocks({ ...live, status: 'interrupted' })[0];
  expect(initial).toMatchObject({ kind: 'tool', effectNotice: null });
  expect(final).toMatchObject({ kind: 'tool', effectNotice: 'reconcile' });
  expect(sameBlock(initial, final)).toBe(false);
  expect(JSON.stringify(live)).toBe(before);
});


it('offers file inspection for a captured error while the containing Turn continues', () => {
  const bound = { ...effect, inspection: { conversationId: 'c', turnId: 'producer',
    operationId: effect.operationId, ownerScope: 'a'.repeat(24) } };
  expect(toolEffectNotice(bound, 'running', 'error')).toBe('reconcile');
  expect(toolEffectNotice({ ...bound, state: 'returned' }, 'running', 'error')).toBe('reconcile');
  expect(toolEffectNotice({ ...bound, state: 'returned' }, 'running', 'done')).toBeNull();
  expect(toolEffectNotice({ ...bound, state: 'not_started' }, 'failed', 'error')).toBeNull();
});
