import { describe, expect, it } from 'vitest';
import { swarmAgentFailureCard, createSwarmFailureNotices } from './swarm-failure-presentation';
const failure = 'Automatic context compaction failed locally before provider dispatch (irreducible_prompt_floor); the prompt remains 115,604 tokens. No main-provider request was sent.';
const t = (key: string) => key;

describe('swarm local context preparation failure', () => {
  it.each([failure, { kind: 'internal', message: 'Internal error', raw: failure }])('explains persisted and structured local failures', error => {
    const card = swarmAgentFailureCard(error, '', 0, t);
    expect(card.title).toBe('swarm.failure.contextPreparation');
    expect(card.recovery).toBe('swarm.failure.contextPreparationRecovery');
    expect(card.detail).toBe(failure);
  });
  it('preserves earlier modifications and never infers that the whole task did no work', () => {
    expect(swarmAgentFailureCard(failure, '', 2, t).recovery)
      .toBe('swarm.failure.contextPreparationWithEdits');
  });
  it('keeps provider prompt rejection and unknown failures distinct', () => {
    expect(swarmAgentFailureCard('Provider context length exceeded', '', 0, t).title)
      .toBe('swarm.failure.generic');
    expect(swarmAgentFailureCard({ code: 'route_unavailable', message: 'No slot available' }, '', 0, t).title)
      .toBe('swarm.failure.noSlot');
  });
  it('uses the same reason in a live notice and deduplicates repeated frames', () => {
    const notices: unknown[] = [];
    const receive = createSwarmFailureNotices(t, notice => notices.push(notice));
    const event = { type: 'swarm_agent_complete', agentId: 'formal', status: 'failed', error: failure };
    receive('conversation', 'run', event, [event]);
    receive('conversation', 'run', event, [event]);
    expect(notices).toHaveLength(1);
    expect(JSON.stringify(notices[0])).toContain('swarm.failure.contextPreparation');
  });
});
