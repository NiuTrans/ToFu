/** Recorded controls, provider metering and readable summaries are independent facts. */
import { describe, expect, it } from 'vitest';
import type { AttemptRecord, TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';

const resolution = { requested: 'preset', requestedValue: 'medium',
  effective: 'preset', actualMode: 'exact', actualValue: 'low' };

function source(projection: TurnProjection = {}, status: TurnRecord['status'] = 'completed'): TurnRecord {
  return { turnId: 'reply', presentationId: 'reply', conversationId: 'reasoning-fixture',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status,
    currentAttemptId: null, projectionRevision: 2, createdAt: 1000, updatedAt: 2000,
    projection: { content: 'Recorded answer', ...projection }, settlement: {} };
}
function select(turn: TurnRecord, attempt?: AttemptRecord) {
  const state = createTurnState(turn.conversationId);
  state.turnsById[turn.turnId] = turn;
  state.laneOrder.main = [turn.turnId];
  if (attempt) state.attemptsById[attempt.attemptId] = attempt;
  return selectConversationViewModel(state).mainLane.turns[0];
}
const reported = { completion_tokens_details: { reasoning_tokens: 2843 } };

function authoring(round: number, actualValue = 'high', reasoning_tokens: unknown = 20) {
  return { round, kind: 'agent', model: 'sol', tag: `R${round}`, usage: {
    reasoning_tokens, _reasoning_resolution: { ...resolution, actualValue },
  } };
}

describe('missing readable reasoning in the common Turn view model', () => {
  it('shows only recorded native effort and provider tokens without manufacturing thinking', () => {
    const turn = source({ model: 'sol', thinkingDepth: 'high', usage: reported,
      lastRoundUsage: { round: 1, reasoningResolution: resolution },
      segments: [{ type: 'thinking', blockId: 'reasoning-empty', text: '', terminal: true }],
    });
    const before = JSON.stringify(turn);
    const vm = select(turn);
    expect(vm.metadata.reasoning).toEqual({ effort: 'low', tokens: 2843,
      tokensSource: 'turn', tokensLowerBound: false, summary: 'unavailable' });
    expect(vm.blocks.some(block => block.kind === 'thinking')).toBe(false);
    expect(JSON.stringify(turn)).toBe(before);
  });

  it.each(['completed', 'truncated', 'failed', 'interrupted'] as const)(
    'keeps %s settlement distinct from provider completion', status => {
      expect(select(source({ usage: reported }, status)).metadata.reasoning?.summary)
        .toBe(status === 'completed' || status === 'truncated' ? 'unavailable' : 'not-received');
    });

  it.each(['pending', 'running'] as const)(
    'uses %s no-summary-yet copy only with recorded reasoning evidence', status => {
      expect(select(source({ thinkingDepth: 'high', model: 'sol' }, status)).metadata.reasoning).toBeUndefined();
      expect(select(source({ usage: reported }, status)).metadata.reasoning?.summary).toBe('not-received-yet');
      expect(select(source({ lastRoundUsage: { reasoningResolution: resolution } }, status))
        .metadata.reasoning).toMatchObject({ effort: 'low', tokens: null, summary: 'not-received-yet' });
      expect(select(source({ usage: { reasoning_tokens: 0 },
        lastRoundUsage: { reasoningResolution: resolution } }, status)).metadata.reasoning)
        .toMatchObject({ effort: 'low', tokens: 0, summary: 'not-received-yet' });
    });

  it('keeps settled receipt-only evidence neutral and marks incomplete metering as a lower bound', () => {
    expect(select(source({ lastRoundUsage: { reasoningResolution: resolution } })).metadata.reasoning)
      .toMatchObject({ effort: 'low', tokens: null, summary: null });
    expect(select(source({ usage: reported, usageComplete: false })).metadata.reasoning)
      .toMatchObject({ tokens: 2843, tokensLowerBound: true, summary: 'unavailable' });
    expect(select(source({ usage: reported, usageComplete: true })).metadata.reasoning?.tokensLowerBound).toBe(false);
  });

  it.each([undefined, null, {}, [], { reasoning_tokens: -1 },
    { reasoning_tokens: 'unknown' }, { reasoning_tokens: '<img src=x>' }])(
    'does not fabricate tokens, effort or a summary from unknown usage %j', usage => {
      expect(select(source({ usage: usage as TurnProjection['usage'],
        thinkingDepth: 'high', cost: { thinkingTokens: 500 },
      })).metadata.reasoning).toBeUndefined();
    });

  it('does not promote aggregate private text, requested/effective labels or provider defaults into actual effort', () => {
    for (const receipt of [
      { ...resolution, actualMode: 'default', actualValue: 'high' },
      { requestedValue: 'high', effective: 'exact' },
      { ...resolution, actualValue: '<img src=x onerror=alert(1)>' },
      { ...resolution, actualValue: 'unreported-new-level' },
    ]) {
      const vm = select(source({ usage: { ...reported, _reasoning_resolution: resolution },
        lastRoundUsage: { reasoningResolution: receipt } }));
      expect(vm.metadata.reasoning).toMatchObject({ effort: null, tokens: 2843 });
    }
    expect(select(source({ lastRoundUsage: { reasoningResolution: {
      actualMode: 'off', actualValue: '', requestedValue: 'high' } } }, 'running'))
      .metadata.reasoning).toMatchObject({ effort: 'off', summary: null });
    expect(select(source({ lastRoundUsage: { reasoningResolution: { actualMode: 'enabled' } } }, 'running'))
      .metadata.reasoning).toMatchObject({ effort: 'enabled', summary: 'not-received-yet' });
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
    expect(vm.metadata.reasoning?.summary).toBe('readable');
    expect(vm.blocks[0]).toMatchObject({ kind: 'thinking', markdown: 'Readable reasoning',
      displayMarkdown: '可读的推理摘要', displayMode: 'translated', terminal: true });
    expect(vm.metadata.translation.available).toBe(true);
    expect(JSON.stringify(turn)).toBe(before);
  });

  it('does not attach model accounting to human inputs or compaction summaries', () => {
    expect(select({ ...source({ usage: reported }), actor: 'human' }).metadata.reasoning).toBeUndefined();
    expect(select(source({ usage: reported, _isCompactionSummary: true })).metadata.reasoning).toBeUndefined();
  });
});

describe('response-authoring reasoning receipt selection', () => {
  it('uses aggregate turn metering independently of the latest round effort, without summing aliases or retries', () => {
    const apiRounds = [authoring(1, 'low', 100), authoring(2, 'high', 70),
      { ...authoring(2, 'max', 999), responseAuthoring: false, tag: 'R2-DISCARDED' },
      { ...authoring(3, 'max', 888), kind: 'compaction' }];
    expect(select(source({ apiRounds, usage: { reasoning_tokens: 170 } })).metadata.reasoning)
      .toMatchObject({ effort: 'high', tokens: 170, tokensSource: 'turn' });
    expect(select(source({ apiRounds })).metadata.reasoning)
      .toMatchObject({ effort: 'high', tokens: 70, tokensSource: 'last-round' });
  });

  it('never searches past a latest authoring snapshot with missing, malformed or stale receipts', () => {
    const apiRounds = [authoring(1), { ...authoring(2), usage: {} }];
    expect(select(source({ apiRounds })).metadata.reasoning).toBeUndefined();
    for (const lastRoundUsage of [{ round: 2 }, null, [], { round: 1, reasoningResolution: resolution }]) {
      expect(select(source({ apiRounds: [authoring(2)], usage: reported,
        lastRoundUsage: lastRoundUsage as TurnProjection['lastRoundUsage'] })).metadata.reasoning?.effort).toBeNull();
    }
    expect(select(source({ apiRounds: [authoring(2)], lastRoundUsage: {
      round: 2, model: 'older-route', reasoningResolution: resolution,
    } })).metadata.reasoning?.effort).toBeNull();
  });

  it('does not reuse inherited reasoning at the base revision of a resumed attempt', () => {
    const turn = { ...source({ usage: reported, lastRoundUsage: { reasoningResolution: resolution } }, 'running'),
      currentAttemptId: 'resumed', projectionRevision: 7 };
    const attempt = { attemptId: 'resumed', conversationId: turn.conversationId, turnId: turn.turnId,
      taskId: 'new-task', operation: 'checkpoint_resume', baseProjectionRevision: 7,
      status: 'running', commandId: 'resume-command', createdAt: 2000, resumeAnchor: {} } as AttemptRecord;
    expect(select(turn, attempt).metadata.reasoning).toBeUndefined();
    const foreign = source({ lastRoundUsage: { reasoningResolution: {
      ...resolution, attemptId: 'old-attempt', taskId: 'old-task',
    } } } as unknown as TurnProjection, 'running');
    foreign.currentAttemptId = attempt.attemptId;
    expect(select(foreign, attempt).metadata.reasoning).toBeUndefined();
  });

  it('keeps bounded historical selection and rejects auxiliary receipt tags', () => {
    const apiRounds = [authoring(1), ...Array.from({ length: 512 }, () => ({ kind: 'compaction' }))];
    expect(select(source({ apiRounds })).metadata.reasoning).toBeUndefined();
    expect(select(source({ lastRoundUsage: { tag: 'R1-DISCARDED', reasoningResolution: resolution } }))
      .metadata.reasoning).toBeUndefined();
  });
});

describe('live resumed-attempt evidence must be current, not merely a newer projection', () => {
  function resumed(operation: AttemptRecord['operation'], status: TurnRecord['status'] = 'pending') {
    const turn = { ...source({ usage: reported, lastRoundUsage: {
      round: 1, reasoningResolution: { ...resolution, actualValue: 'high' },
    } }, status), currentAttemptId: 'resumed', projectionRevision: 8 };
    const attempt = { attemptId: 'resumed', conversationId: turn.conversationId, turnId: turn.turnId,
      taskId: 'new-task', operation, baseProjectionRevision: 7, status: 'pending',
      commandId: 'resume-command', createdAt: 2000, resumeAnchor: {} } as AttemptRecord;
    return { turn, attempt };
  }
  it.each(['continue', 'checkpoint_resume', 'regenerate', 'answer_guidance'] as const)(
    'keeps inherited usage/resolution suppressed for %s ACK and subsequent unrelated updates', operation => {
      const { turn, attempt } = resumed(operation);
      for (const projectionRevision of [8, 9, 16]) {
        for (const status of ['pending', 'running'] as const) {
          const update = { ...turn, projectionRevision, status, projection: { ...turn.projection,
            content: `Unrelated content delta ${projectionRevision}`, todoState: { update: projectionRevision } } };
          expect(select(update, attempt).metadata.reasoning).toBeUndefined();
        }
      }
    });
  it('admits a current scoped control without borrowing the inherited aggregate token count', () => {
    const { turn, attempt } = resumed('checkpoint_resume', 'running');
    const projection = { ...turn.projection, lastRoundUsage: { round: 1, reasoningResolution: {
      ...resolution, attemptId: attempt.attemptId, taskId: attempt.taskId,
    } } } as unknown as TurnProjection;
    expect(select({ ...turn, projection }, attempt).metadata.reasoning)
      .toMatchObject({ effort: 'low', tokens: null, summary: 'not-received-yet' });
  });
  it('uses only the provably current authoring round count during live resume, never a colliding old counter', () => {
    const { turn, attempt } = resumed('checkpoint_resume', 'running');
    const row = { ...authoring(1, 'low', 20), attemptId: attempt.attemptId, taskId: attempt.taskId };
    expect(select({ ...turn, projection: { ...turn.projection, apiRounds: [row] } }, attempt).metadata.reasoning)
      .toMatchObject({ effort: null, tokens: 20, tokensSource: 'last-round', summary: 'not-received-yet' });
    expect(select({ ...turn, projection: { ...turn.projection, apiRounds: [authoring(2)] } }, attempt)
      .metadata.reasoning).toBeUndefined();
    expect(select({ ...turn, projection: { ...turn.projection, apiRounds: [{ ...row, taskId: 'old-task' }] } }, attempt)
      .metadata.reasoning).toBeUndefined();
  });
});
