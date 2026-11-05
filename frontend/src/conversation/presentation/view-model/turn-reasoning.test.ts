/** Removing reply-header notices never removes provider evidence or readable thinking. */
import { describe, expect, it } from 'vitest';
import type { TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';

const resolution = { requested: 'preset', requestedValue: 'medium',
  effective: 'preset', actualMode: 'exact', actualValue: 'low' };
const reported = { completion_tokens_details: { reasoning_tokens: 2843 } };
function source(projection: TurnProjection = {}, status: TurnRecord['status'] = 'completed'): TurnRecord {
  return { turnId: 'reply', presentationId: 'reply', conversationId: 'reasoning-fixture',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status,
    currentAttemptId: null, projectionRevision: 2, createdAt: 1000, updatedAt: 2000,
    projection: { content: 'Recorded answer', ...projection }, settlement: {} };
}
function select(turn: TurnRecord) {
  const state = createTurnState(turn.conversationId);
  state.turnsById[turn.turnId] = turn;
  state.laneOrder.main = [turn.turnId];
  return selectConversationViewModel(state).mainLane.turns[0];
}

describe('reasoning evidence without a reply-header status projection', () => {
  it.each(['pending', 'running', 'completed', 'truncated', 'failed', 'interrupted'] as const)(
    'retains metering and request receipts for %s replies without manufacturing thinking', status => {
      const turn = source({ thinkingDepth: 'high', usage: reported, usageComplete: false,
        lastRoundUsage: { round: 1, reasoningResolution: resolution },
        segments: [{ type: 'thinking', blockId: 'reasoning-empty', text: '', terminal: true }],
      }, status);
      const before = JSON.stringify(turn);
      const vm = select(turn);
      expect(vm.metadata.usage).toBe(turn.projection.usage);
      expect(vm.metadata.lastRoundUsage).toBe(turn.projection.lastRoundUsage);
      expect(vm.metadata.thinkingDepth).toBe('high');
      expect(Object.hasOwn(vm.metadata, 'reasoning')).toBe(false);
      expect(vm.blocks.some(block => block.kind === 'thinking')).toBe(false);
      expect(JSON.stringify(turn)).toBe(before);
    });

  it('preserves readable thinking, recorded translations and their existing lifecycle', () => {
    const turn = source({ usage: reported, lastRoundUsage: { reasoningResolution: resolution },
      thinking: 'Readable reasoning', segments: [
        { type: 'thinking', blockId: 'reasoning-readable', text: 'Readable reasoning',
          terminal: true, translatedText: '可读的推理摘要' },
        { type: 'text', blockId: 'answer', text: 'Recorded answer', terminal: true, deliverable: true },
      ] });
    const before = JSON.stringify(turn);
    const vm = select(turn);
    expect(vm.blocks[0]).toMatchObject({ kind: 'thinking', markdown: 'Readable reasoning',
      displayMarkdown: '可读的推理摘要', displayMode: 'translated', terminal: true });
    expect(vm.metadata.translation.available).toBe(true);
    expect(vm.metadata.usage).toBe(turn.projection.usage);
    expect(vm.metadata.lastRoundUsage).toBe(turn.projection.lastRoundUsage);
    expect(JSON.stringify(turn)).toBe(before);
  });

  it('renders signed and snapshot-slimmed reasoning as the same presentation', () => {
    // Completed snapshots omit the opaque provider signature; a live-settled
    // Turn may still carry it. The thinking block never depends on it.
    const segment = { type: 'thinking' as const, blockId: 'reasoning-signed',
      text: 'Signed reasoning', terminal: false };
    const signed = select(source({ segments: [{ ...segment, signature: 'opaque-provider-signature' }] }));
    const slim = select(source({ segments: [segment] }));
    const { source: signedSource, ...signedBlock } = signed.blocks[0];
    const { source: slimSource, ...slimBlock } = slim.blocks[0];
    expect(signedSource).toMatchObject({ signature: 'opaque-provider-signature' });
    expect(slimSource).not.toHaveProperty('signature');
    expect(signedBlock).toEqual(slimBlock);
    expect(signedBlock).not.toHaveProperty('signature');
  });

  it('keeps authoring, retry and auxiliary billing evidence unchanged for existing diagnostics', () => {
    const round = (round: number, actualValue: string, tokens: number) => ({ round, kind: 'agent', usage: {
      reasoning_tokens: tokens, _reasoning_resolution: { ...resolution, actualValue },
    } });
    const turn = source({ apiRounds: [round(1, 'low', 100), round(2, 'high', 70),
      { ...round(2, 'max', 999), responseAuthoring: false, tag: 'R2-DISCARDED' },
      { ...round(3, 'max', 888), kind: 'compaction' }], usage: { reasoning_tokens: 170 },
      lastRoundUsage: { round: 2, reasoningResolution: { ...resolution, actualValue: 'high' } },
    });
    const before = JSON.stringify(turn);
    const vm = select(turn);
    expect(vm.source.projection.apiRounds).toBe(turn.projection.apiRounds);
    expect(vm.metadata.usage).toEqual({ reasoning_tokens: 170 });
    expect(vm.metadata.lastRoundUsage?.reasoningResolution?.actualValue).toBe('high');
    expect(JSON.stringify(turn)).toBe(before);
  });
});
