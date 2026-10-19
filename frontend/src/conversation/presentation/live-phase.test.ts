import { describe, expect, it } from 'vitest';
import type { TransientTurnPresentation } from '../domain/transient-turn';
import { liveAttemptPresentation } from './view-model/live-turns';
import { liveStatusPhase, presentLivePhaseLabel } from './live-phase';
const text = (key: string, fallback: string, args?: Readonly<Record<string, string | number>>) =>
  (key ? `localized:${key}` : fallback) + (args ? JSON.stringify(args) : '');
function root(currentPhase: TransientTurnPresentation): TransientTurnPresentation {
  return { kind: 'attempt', phase: 'waiting', label: '', detail: '',
    goalProgress: { iteration: '4', role: 'worker', rootSummary: true, workers: [], currentPhase } };
}
describe('shared live phase presentation', () => {
  it.each([
    { phase: 'waiting_model' }, { phase: 'llm_thinking' }, { phase: 'thinking_active' },
    { phase: 'retrying' }, { phase: 'tool_exec', tools: ['read_file', 'inspect_runtime'] },
    { phase: 'worker_starting', detailKey: 'stream.phase.workerStarting' },
    { phase: 'retrying', detailKey: 'stream.phase.retryRateLimited', detailArgs: { model: 'm' } },
    { phase: 'llm_thinking', detailKey: 'stream.phase.analyzingRound', detailArgs: { round: 4 }, detail: 'English' },
    { phase: 'third_party', detail: 'Plugin progress' },
  ])('uses identical normal and Goal root copy for $phase', raw => {
    const value = liveAttemptPresentation(raw, 'running');
    expect(presentLivePhaseLabel(liveStatusPhase(root(value)), text)).toBe(presentLivePhaseLabel(value, text));
  });
  it('keeps tool identity and routing evidence in Goal root context', () => {
    const current = liveAttemptPresentation({ phase: 'tool_exec', tools: ['canonical_tool'], modelRoute: {
      selectedModel: 'selected', resolvedModel: 'resolved', role: 'worker', tier: 'heavy', kind: 'role_tier',
    } }, 'running');
    const label = presentLivePhaseLabel(liveStatusPhase(root(current)), text);
    expect(label).toContain('canonical_tool');
    expect(label).toContain('stream.phase.modelRouted');
    expect(label).toContain('resolved');
  });
  it('never replaces a root storage warning with a joined worker phase', () => {
    const value = { ...root(liveAttemptPresentation({ phase: 'waiting_model' }, 'running')), phase: 'storage_wedged' };
    expect(liveStatusPhase(value)).toBe(value);
    expect(presentLivePhaseLabel(liveStatusPhase(value), text)).toBe('localized:stream.phase.storageWedged');
  });
  it('keeps executor preparing/queued distinct and rejects stale queue status after entry', () => {
    expect(liveAttemptPresentation({}, 'pending').phase).toBe('executor_preparing');
    expect(liveAttemptPresentation({ phase: 'executor_queued' }, 'pending').phase).toBe('executor_queued');
    expect(liveAttemptPresentation({ phase: 'executor_queued' }, 'running').phase).toBe('waiting');
  });
});
