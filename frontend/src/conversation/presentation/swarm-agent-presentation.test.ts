import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en.json';
import zh from '../../i18n/locales/zh.json';
import { swarmAgentPhaseLabel, type SwarmLabelText } from './swarm-agent-presentation';

const EN = en as Record<string, string>;
const ZH = zh as Record<string, string>;
const localized = (messages: Record<string, string>): SwarmLabelText => (key, _fallback, args) => {
  const template = messages[key];
  if (template === undefined) throw new Error(`missing locale key ${key}`);
  return template.replace(/\{(\w+)\}/g, (match, name) => String(args?.[name] ?? match));
};

// Chat-domain PHASE values a worker's own chat loop forwards
// (lib/agent_core/events.py `Phase`), and the swarm's own words.
const forwardedPhases = ['llm_thinking', 'tool_exec', 'retrying', 'waiting_model', 'stream_stalled',
  'executor_queued', 'working', 'compacting', 'todo_continuation', 'intent_stall_nudge',
  'tool_history_restored', 'tool_authority'];
const swarmPhases = ['running', 'starting', 'thinking', 'tool_use', 'writing', 'searching',
  'coding', 'analyzing'];

describe('swarm agent status words', () => {
  for (const [language, messages] of [['en', EN], ['zh', ZH]] as const) {
    it(`gives every running state one localized word in ${language}`, () => {
      const tt = localized(messages);
      const word = (phase: string) => swarmAgentPhaseLabel({ status: 'running', phase }, tt);
      for (const phase of [...forwardedPhases, ...swarmPhases, 'a_future_phase', '']) {
        expect(word(phase)).toBeTruthy();
        expect(word(phase)).not.toBe(phase);
        expect(word(phase)).not.toMatch(/^[a-z]+(_[a-z]+)+$/);
      }
      expect(word('tool_exec')).toBe(word('tool_use'));
      expect(word('llm_thinking')).toBe(word('thinking'));
      expect(word('waiting_model')).toBe(messages['swarm.phase.waitingModel']);
      expect(word('retrying')).toBe(messages['swarm.phase.retrying']);
      expect(word('stream_stalled')).toBe(messages['swarm.phase.streamStalled']);
      expect(word('compacting')).toBe(messages['swarm.phase.compacting']);
      expect(word('a_future_phase')).toBe(messages['swarm.phase.running']);
    });
  }

  it('lets terminal and waiting status outrank a phase stranded at a live value', () => {
    const tt = localized(EN);
    expect(swarmAgentPhaseLabel({ status: 'done', phase: 'tool_exec' }, tt)).toBe(EN['swarm.phase.complete']);
    expect(swarmAgentPhaseLabel({ status: 'failed', phase: 'waiting_model' }, tt)).toBe(EN['swarm.phase.failed']);
    expect(swarmAgentPhaseLabel({ status: 'cancelled', phase: 'waiting_model' }, tt)).toBe(EN['swarm.phase.aborted']);
    expect(swarmAgentPhaseLabel({ status: 'interrupted', phase: 'tool_use' }, tt)).toBe(EN['swarm.phase.interrupted']);
    expect(swarmAgentPhaseLabel({ status: 'pending', phase: 'pending', waitReason: 'capacity' }, tt))
      .toBe(EN['swarm.wait.capacity']);
    expect(swarmAgentPhaseLabel({ status: 'pending' }, tt)).toBe(EN['swarm.wait.pending']);
    expect(swarmAgentPhaseLabel({ status: 'stalled', stallSilentSeconds: 312.4 }, tt)).toBe('Stalled · silent 312s');
    expect(swarmAgentPhaseLabel({ status: 'stalled' }, tt)).toBe(EN['swarm.phase.stalled']);
    expect(swarmAgentPhaseLabel({ status: 'unknown', phase: 'unknown' }, tt)).toBe(EN['swarm.phase.noResult']);
  });

  it('reads a phase-only chat frame as worker evidence, not a missing result', () => {
    expect(swarmAgentPhaseLabel({ phase: 'waiting_model' }, localized(EN))).toBe(EN['swarm.phase.waitingModel']);
  });
});
