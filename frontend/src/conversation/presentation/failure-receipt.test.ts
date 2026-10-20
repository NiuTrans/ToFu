import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { errorFailureAttribution, normalizeErrorEnvelope } from '../../api/errors';
import { createTurnState } from '../../core/turn-state';
import { selectConversationViewModel } from './conversation-view-model';

function failedTurn(error: Record<string, unknown>): TurnRecord {
  return {
    turnId: 'turn-fail-1',
    actor: 'assistant',
    status: 'failed',
    currentAttemptId: 'attempt-7',
    projectionRevision: 3,
    projection: {},
    settlement: { outcome: 'failed', cause: 'generation_error', error },
  } as unknown as TurnRecord;
}

function stateWith(turn: TurnRecord) {
  const state = createTurnState('conv-1');
  state.turnsById[turn.turnId] = turn as never;
  state.laneOrder.main = [turn.turnId];
  return state;
}

describe('errorFailureAttribution', () => {
  it('narrows the additive failureAttribution extension', () => {
    const envelope = normalizeErrorEnvelope({
      kind: 'tool_loop',
      message: 'tool loop',
      failureAttribution: {
        roundNum: 4, phase: 'nonretryable_tool_failure',
        toolName: 'bash', toolCallId: 'call-1',
      },
    });
    expect(errorFailureAttribution(envelope)).toEqual({
      roundNum: 4,
      phase: 'nonretryable_tool_failure',
      toolName: 'bash',
      toolCallId: 'call-1',
    });
  });

  it('drops malformed or empty attribution payloads', () => {
    expect(errorFailureAttribution(
      normalizeErrorEnvelope({ kind: 'generic', message: 'x' }),
    )).toBeNull();
    expect(errorFailureAttribution(
      normalizeErrorEnvelope({
        kind: 'generic', message: 'x',
        failureAttribution: { roundNum: 'four', toolName: 3 },
      }),
    )).toBeNull();
    expect(errorFailureAttribution(null)).toBeNull();
  });
});

describe('terminal failure receipt view-model', () => {
  it('surfaces the failed settlement envelope with its attribution on the '
    + 'terminal-error block', () => {
    const envelope = {
      kind: 'ratelimit',
      message: 'rate limited',
      failureAttribution: { roundNum: 2, toolName: 'bash' },
    };
    const viewModel = selectConversationViewModel(stateWith(failedTurn(envelope)));
    const terminalBlock = viewModel.mainLane.turns[0]?.blocks.find(
      (block) => block.kind === 'activity-event' && block.terminalError,
    );
    expect(terminalBlock).toBeDefined();
    expect(
      errorFailureAttribution(
        (terminalBlock as { terminalError?: never }).terminalError,
      ),
    ).toEqual({ roundNum: 2, toolName: 'bash' });
    expect(viewModel.mainLane.turns[0]?.attemptId).toBe('attempt-7');
  });

  it('does not fabricate a terminal block for a healthy turn', () => {
    const turn = failedTurn({ kind: 'generic', message: 'x' });
    (turn as { status: string }).status = 'completed';
    const viewModel = selectConversationViewModel(stateWith(turn));
    expect(
      viewModel.mainLane.turns[0]?.blocks.some(
        (block) => block.kind === 'activity-event' && block.terminalError,
      ),
    ).toBeFalsy();
  });
});

describe('transportNotice', () => {
  it.each(['recovering', 'degraded', 'offline'] as const)(
    'exposes the %s sync state after the debounce window',
    (healthState) => {
      const state = stateWith(failedTurn({ kind: 'generic', message: 'x' }));
      /* First call starts the debounce timer; still null. */
      let viewModel = selectConversationViewModel(
        state, {}, { syncHealth: { state: healthState } },
      );
      expect(viewModel.transportNotice).toBeNull();
      /* Fast-forward past the 1 s debounce. */
      const originalNow = Date.now;
      Date.now = () => originalNow() + 1100;
      try {
        viewModel = selectConversationViewModel(
          state, {}, { syncHealth: { state: healthState } },
        );
        expect(viewModel.transportNotice).toBe(healthState);
      } finally {
        Date.now = originalNow;
      }
    },
  );

  it.each(['live', 'idle', 'connecting', 'closed'])(
    'stays silent while the sync channel is %s',
    (healthState) => {
      const viewModel = selectConversationViewModel(
        stateWith(failedTurn({ kind: 'generic', message: 'x' })),
        {},
        { syncHealth: { state: healthState } },
      );
      expect(viewModel.transportNotice).toBeNull();
    },
  );

  it('stays silent without any published health', () => {
    const viewModel = selectConversationViewModel(
      stateWith(failedTurn({ kind: 'generic', message: 'x' })),
    );
    expect(viewModel.transportNotice).toBeNull();
  });
});
