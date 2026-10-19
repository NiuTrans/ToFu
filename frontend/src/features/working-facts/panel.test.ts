// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { mountWorkingFacts } from './panel';
import type { WorkingFactsPorts } from './ports';
import type { WorkingFactsView, WorkingFactConsumersView } from '../../api/working-facts.generated';

const text = (_: string, fallback: string) => fallback;
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const facts = (revision = 1): WorkingFactsView => ({ schemaVersion: 1, conversationId: 'conv', revision,
  semantics: 'source_binding_and_owner_assessment_not_automatic_truth', facts: [{
    id: 'fact', revision, text: revision === 1 ? '<script>Report 1</script>' : 'Report 2', kind: 'observation',
    sources: [{ kind: 'artifact', id: 'artifact', version: 'a'.repeat(64), quote: 'Report 1' }],
    active: true, actor: 'agent', subject: 'agent', producerTaskId: 'producer', note: '', createdAt: 1, updatedAt: 1,
    status: 'observed', sourceStates: [], reasons: [],
  }] });
const impact = (revision = 1): WorkingFactConsumersView => ({ schemaVersion: 1, conversationId: 'conv', factsRevision: revision,
  coverage: 'admitted_working_fact_context_since_enablement', semantics: 'prepared_and_provider_confirmed_dependencies_not_task_acceptance', nextCursor: '',
  consumers: [{ taskId: 'task', turnId: 'turn', attemptId: 'attempt', requiresRevalidation: revision > 1, consumptionUncertain: true, updatedAt: 1,
    dependencies: [{ id: 'fact', preparedFirstRevision: 1, preparedLastRevision: 1, confirmedFirstRevision: null, confirmedLastRevision: null,
      currentRevision: revision, status: 'unconfirmed', reasons: ['provider_consumption_not_fully_confirmed'] }] }] });
const disposals: Array<() => void> = [];
afterEach(() => { disposals.splice(0).forEach(fn => fn()); document.body.replaceChildren(); });
function setup(overrides: Partial<WorkingFactsPorts> = {}) {
  const host = document.createElement('div'); document.body.append(host);
  let current = facts();
  const ports = { load: vi.fn(async () => current), save: vi.fn(async () => { current = facts(2); return current; }), consumers: vi.fn(async () => impact()), ...overrides };
  const dispose = mountWorkingFacts(host, 'conv', ports, text); disposals.push(dispose);
  return { host, ports, dispose };
}
function click(host: HTMLElement, label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent === label);
  if (!button) throw new Error('Missing control: ' + label);
  button.click();
}
async function edit(host: HTMLElement, value = 'Correct report', note = 'Checked source') {
  host.querySelector<HTMLDetailsElement>('[data-working-fact]')!.open = true; await settle();
  const fields = host.querySelectorAll<HTMLInputElement>('input');
  for (const [index, text] of [value, note].entries()) { fields[index].value = text; fields[index].dispatchEvent(new Event('input')); }
}

it('uses owner-bound versions, renders source text safely and refreshes impact only after acknowledgement', async () => {
  const consumers = vi.fn().mockResolvedValueOnce(impact()).mockResolvedValueOnce(impact(2));
  const { host, ports } = setup({ consumers }); await settle();
  expect(ports.load).toHaveBeenCalledWith('conv', expect.any(AbortSignal));
  expect(host.querySelector('script')).toBeNull();
  await edit(host); click(host, 'Save correction'); await settle();
  expect(ports.save).toHaveBeenCalledWith('conv', { action: 'correct', id: 'fact', revision: 1, text: 'Correct report', note: 'Checked source' }, expect.any(AbortSignal));
  expect(host.textContent).toContain('Owner decision saved'); expect(host.textContent).toContain('Needs revalidation');
  expect(host.querySelector('[data-fact-consumer] summary')?.textContent).toContain('Use not fully confirmed');
  expect(consumers).toHaveBeenCalledTimes(2);
});

it('requires a reason and refuses empty correction text without sending a command', async () => {
  const { host, ports } = setup(); await settle(); await edit(host, '', ''); click(host, 'Save correction');
  expect(ports.save).not.toHaveBeenCalled(); expect(host.textContent).toContain('A reason is required');
});

it('identifies an ordinary Turn criterion without confusing it with an artifact source', async () => {
  const current = facts();
  current.facts[0].sources = [{ kind: 'criterion', targetKind: 'turn', id: 'turn-owned',
    criterionId: 'budget', version: 'a'.repeat(64), quote: 'Budget: 100' }];
  const { host } = setup({ load: vi.fn(async () => current) }); await settle();
  await edit(host);
  expect(host.textContent).toContain('Turn · criterion: turn-owned / budget');
  expect(host.querySelector('blockquote')?.textContent).toBe('Budget: 100');
});

it('refreshes the entire graph after a selected-fact response and keeps independent rows', async () => {
  const initial = facts(); initial.facts.push({ ...initial.facts[0], id: 'derived', kind: 'derived', status: 'needs_review' },
    { ...initial.facts[0], id: 'independent' });
  const refreshed = { ...facts(2), facts: [facts(2).facts[0], { ...initial.facts[1], status: 'stale' as const }, initial.facts[2]] };
  const load = vi.fn().mockResolvedValueOnce(initial).mockResolvedValueOnce(refreshed);
  const { host } = setup({ load, save: vi.fn(async () => facts(2)) }); await settle();
  await edit(host); click(host, 'Save correction'); await settle();
  expect(host.querySelectorAll('[data-working-fact]')).toHaveLength(3);
  expect(host.querySelector('[data-working-fact="derived"] summary')?.textContent).toContain('Source changed');
  expect(host.querySelector('[data-working-fact="independent"]')).not.toBeNull();
  expect(load).toHaveBeenCalledTimes(2);
});

it.each(['unavailable', 'older'])('keeps a confirmed decision distinct from an %s full-graph refresh', async outcome => {
  const load = vi.fn().mockResolvedValueOnce(facts());
  if (outcome === 'older') load.mockResolvedValueOnce(facts());
  else load.mockRejectedValueOnce(new Error('offline'));
  const { host, ports } = setup({ load }); await settle(); await edit(host);
  click(host, 'Save correction'); await settle();
  expect(host.textContent).toContain('Owner decision saved');
  expect(host.textContent).toContain('Facts could not be loaded');
  expect(host.textContent).not.toContain('Decision was not confirmed');
  expect(host.querySelector('fieldset')?.disabled).toBe(true);
  click(host, 'Save correction'); expect(ports.save).toHaveBeenCalledTimes(1);
});

it('refreshes untouched correction defaults when another owner changes a fact', async () => {
  const load = vi.fn().mockResolvedValueOnce(facts()).mockResolvedValueOnce(facts(2));
  const { host } = setup({ load }); await settle();
  host.querySelector<HTMLDetailsElement>('[data-working-fact]')!.open = true; await settle();
  click(host, 'Reload latest facts and impact'); await settle(); await settle();
  expect(host.querySelector('input')?.value).toBe('Report 2');
});

it('preserves drafts across conflict/reload and requires the refreshed aggregate revision', async () => {
  const load = vi.fn().mockResolvedValueOnce(facts()).mockResolvedValueOnce(facts(5)).mockResolvedValueOnce(facts(6));
  const save = vi.fn().mockRejectedValueOnce(new Error('409 changed')).mockResolvedValueOnce(facts(6));
  const { host } = setup({ load, save }); await settle(); await edit(host); click(host, 'Save correction'); await settle();
  expect(host.querySelector('input')?.value).toBe('Correct report'); expect(host.textContent).toContain('Decision was not confirmed');
  click(host, 'Save correction'); expect(save).toHaveBeenCalledTimes(1);
  click(host, 'Reload latest facts and impact'); await settle(); await settle();
  expect(host.textContent).toContain('Report 2'); expect(host.querySelector('input')?.value).toBe('Correct report');
  click(host, 'Save correction'); await settle();
  expect(save.mock.calls[1][1]).toMatchObject({ revision: 5, text: 'Correct report', note: 'Checked source' });
});

it('clears old impact after a saved correction even when the impact query fails', async () => {
  const consumers = vi.fn().mockResolvedValueOnce(impact()).mockRejectedValueOnce(new Error('offline'));
  const { host } = setup({ consumers }); await settle(); await edit(host); click(host, 'Save correction'); await settle();
  expect(host.textContent).toContain('Owner decision saved'); expect(host.textContent).toContain('Task impact could not be loaded');
  expect(host.querySelector('[data-fact-consumer]')).toBeNull();
});

it('keeps an unavailable fact read distinct from an empty fact list and forbids decisions', async () => {
  const { host, ports } = setup({ load: vi.fn(async () => { throw new Error('403 forbidden'); }) }); await settle();
  expect(host.textContent).toContain('Facts could not be loaded'); expect(host.textContent).not.toContain('No source-bound');
  expect(host.querySelector('fieldset')?.disabled).toBe(true); expect(ports.save).not.toHaveBeenCalled();
});

it('rejects cross-conversation payloads and fences late reads after disposal', async () => {
  const first = setup({ load: vi.fn(async () => ({ ...facts(), conversationId: 'foreign' })) }); await settle();
  expect(first.host.textContent).toContain('another conversation'); expect(first.host.querySelector('[data-working-fact]')).toBeNull();
  let resolve!: (value: WorkingFactsView) => void;
  const load = vi.fn((_id: string, _signal: AbortSignal) => new Promise<WorkingFactsView>(done => { resolve = done; }));
  const second = setup({ load }); second.dispose(); resolve(facts()); await settle();
  expect(load.mock.calls[0][1].aborted).toBe(true); expect(second.host.childElementCount).toBe(0);
});

it('never repeats an in-flight or ambiguously acknowledged write and retires late acknowledgements', async () => {
  let resolve!: (value: WorkingFactsView) => void;
  const save = vi.fn(() => new Promise<WorkingFactsView>(done => { resolve = done; }));
  const { host, ports, dispose } = setup({ save }); await settle(); await edit(host);
  click(host, 'Save correction'); click(host, 'Save correction'); expect(save).toHaveBeenCalledTimes(1);
  dispose(); resolve(facts(2)); await settle(); expect(ports.consumers).toHaveBeenCalledTimes(1); expect(host.childElementCount).toBe(0);
});

it('bounds fact DOM, retains drafts across pages, replaces consumer pages and exposes mixed read versions', async () => {
  const many = facts(); many.facts = Array.from({ length: 41 }, (_, i) => ({ ...many.facts[0], id: 'f' + i }));
  const consumers = vi.fn().mockResolvedValueOnce({ ...impact(), nextCursor: 'b'.repeat(64) }).mockResolvedValueOnce({ ...impact(2), consumers: [] });
  const { host } = setup({ load: vi.fn(async () => many), consumers }); await settle();
  expect(host.querySelectorAll('[data-working-fact]')).toHaveLength(20); await edit(host);
  click(host, 'Next facts'); expect(host.querySelectorAll('[data-working-fact]')).toHaveLength(20);
  click(host, 'Previous facts'); await settle(); expect(host.querySelector('input')?.value).toBe('Correct report');
  click(host, 'Next task page'); await settle();
  expect(consumers.mock.calls[1][1]).toBe('b'.repeat(64)); expect(host.querySelectorAll('[data-fact-consumer]')).toHaveLength(0);
  expect(host.textContent).toContain('Facts changed during this read'); expect(host.textContent).toContain('Missing tasks may still be affected');
});

it('renders a physical file source path as text alongside its exact version and quote', async () => {
  const view = facts();
  view.facts[0].sources = [{ kind: 'file', id: 'file_123', version: 'b'.repeat(64), path: '/project/<img>.txt', quote: 'Budget: 100' }];
  const { host } = setup({ load: async () => view }); await settle();
  host.querySelector('summary')?.click(); await settle();
  expect(host.textContent).toContain('/project/<img>.txt');
  expect(host.textContent).toContain('b'.repeat(64));
  expect(host.textContent).toContain('Budget: 100');
  expect(host.querySelector('img')).toBeNull();
});


it('labels tool evidence as historical with timestamp and coverage, using text-only rendering', async () => {
  const view = facts();
  view.facts[0].sources = [{ kind: 'tool_result', id: 'tool_123', version: 'c'.repeat(64), quote: 'Budget: 100',
    toolName: '<img>tool', toolCallId: 'observation', observedAt: 1792000000000,
    sourceCoverage: 'partial', temporality: 'historical_execution' }];
  const { host } = setup({ load: async () => view }); await settle();
  host.querySelector('summary')?.click(); await settle();
  expect(host.textContent).toContain('Historical tool observation');
  expect(host.textContent).toContain('Original source coverage: partial');
  expect(host.querySelector('time')?.getAttribute('datetime')).toBe(new Date(1792000000000).toISOString());
  expect(host.textContent).toContain('<img>tool'); expect(host.querySelector('img')).toBeNull();
});
