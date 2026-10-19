import { describe, expect, it, vi } from 'vitest';
import { createAuthenticatedChoiceInteractionPorts, createChoiceInteractionPorts } from './choice-interaction';
import { conversationSyncApi } from '../../api/conversation-sync.generated';
vi.mock('../../api/conversation-sync.generated', () => ({
  conversationSyncApi: { turn: vi.fn(), createAttempt: vi.fn() },
}));

function live(receipt = false): any {
  return { conversationId: 'owned', turnId: 'turn', status: 'running', projectionRevision: 7,
    projection: { toolRounds: [{ toolName: 'ask_human', guidanceId: 'hg_live',
      status: receipt ? 'completed' : 'awaiting_human', guidanceType: 'choice',
      guidanceQuestion: 'Choose', guidanceOptions: ['Client', 'Server'],
      ...(receipt ? { guidanceResponse: 'Server', guidanceAnswerCommandId: 'cmd' } : {}) }] } };
}
const signal = () => new AbortController().signal;
const attempt = { turnId: 'turn', commandId: 'original-generate', operation: 'generate' };

describe('scoped live choice command adapter', () => {

  it('production ports call only trusted generated scoped turn/attempt helpers', async () => {
    vi.mocked(conversationSyncApi.turn).mockResolvedValue({ turn: live() } as any);
    vi.mocked(conversationSyncApi.createAttempt).mockResolvedValue({ turn: live(true), attempt } as any);
    const ports = createAuthenticatedChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn', commandId: () => 'cmd' });
    const abort = signal();
    expect((await ports.load('hg_live', abort)).state).toBe('pending');
    expect((await ports.submit('hg_live', 'Server', 'cmd', abort)).state).toBe('answered');
    expect(conversationSyncApi.turn).toHaveBeenCalledWith('owned', 'turn', { signal: abort });
    expect(conversationSyncApi.createAttempt).toHaveBeenCalledWith('owned', 'turn', {
      commandId: 'cmd', operation: 'answer_guidance', expectedProjectionRevision: 7,
      humanResponse: 'Server', guidanceId: 'hg_live', config: {},
    }, { signal: abort });
  });
  it('projects a running ask_human as pending and requires its durable receipt', async () => {
    let current = live();
    const createAttempt = vi.fn(async () => { current = live(true); return { turn: current, attempt }; });
    const ports = createChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn',
      readTurn: async () => current, createAttempt, commandId: () => 'cmd' });
    expect((await ports.load('hg_live', signal())).state).toBe('pending');
    expect((await ports.submit('hg_live', 'Server', 'cmd', signal())).state).toBe('answered');
    expect(createAttempt).toHaveBeenCalledWith('owned', 'turn', {
      commandId: 'cmd', operation: 'answer_guidance', expectedProjectionRevision: 7,
      humanResponse: 'Server', guidanceId: 'hg_live', config: {},
    }, expect.any(AbortSignal));
    // Reload proves acceptance without the in-process waiter or settlement.
    expect((await ports.load('hg_live', signal())).response).toBe('Server');
  });
  it('rejects a volatile waiter ACK with no persisted receipt', async () => {
    const ports = createChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn',
      readTurn: async () => live(), createAttempt: async () => ({ turn: live(), attempt }), commandId: () => 'cmd' });
    await expect(ports.submit('hg_live', 'Server', 'cmd', signal())).rejects.toThrow('durably accepted');
  });
  it('retries the identical scoped command after lost ACK and concurrent streaming', async () => {
    let current = live(), calls = 0;
    const createAttempt = vi.fn(async () => {
      current = live(true); current.projectionRevision = 11;
      if (!calls++) { current = live(); current.projectionRevision = 11; throw Error('lost ACK'); }
      return { turn: current, attempt };
    });
    const ports = createChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn',
      readTurn: async () => current, createAttempt, commandId: () => 'cmd' });
    await expect(ports.submit('hg_live', 'Server', 'cmd', signal())).rejects.toThrow('lost ACK');
    expect((await ports.submit('hg_live', 'Server', 'cmd', signal())).response).toBe('Server');
    expect(createAttempt.mock.calls[0].slice(0, 3)).toEqual(createAttempt.mock.calls[1].slice(0, 3));
  });
  it('treats absent/foreign interactions as not ready and stopped unresolved ones as stale', async () => {
    const current = live(); current.status = 'stopped';
    const createAttempt = vi.fn();
    const ports = createChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn',
      readTurn: async () => current, createAttempt, commandId: () => 'cmd' });
    expect((await ports.load('hg_live', signal())).state).toBe('stale');
    await expect(ports.load('foreign', signal())).rejects.toThrow('not owned');
    expect(createAttempt).not.toHaveBeenCalled();
  });
});

it.each(['running', 'stopped'])('preserves one-option declarations and explanations for %s turns', async status => {
  const turn = live(); turn.status = status;
  turn.projection.toolRounds[0].guidanceOptions = [{ label: 'Continue', description: 'Retain supplied explanation' }];
  if (status === 'stopped') turn.settlement = { resumeOptions: [{
    operation: 'answer_guidance', anchor: { guidanceId: 'hg_live' },
  }] };
  const ports = createChoiceInteractionPorts({ conversationId: 'owned', turnId: 'turn',
    readTurn: async () => turn, createAttempt: async () => ({}), commandId: () => 'cmd' });
  const question = await ports.load('hg_live', signal());
  expect(question.options).toEqual(['Continue']);
  expect(question.descriptions).toEqual(['Retain supplied explanation']);
  expect(question.state).toBe('pending');
});
