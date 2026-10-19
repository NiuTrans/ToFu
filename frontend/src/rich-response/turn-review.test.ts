// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { mountTurnReview } from './artifact-review';
import type { TurnReviewView } from '../api/artifact-review.generated';

const value: TurnReviewView = { schemaVersion: 1, conversationId: 'conv', turnId: 'turn',
  requirementsRevision: 1, decisionsRevision: 0, objective: 'Deliver a report', executionStatus: 'running',
  criteria: [{ id: 'correct', text: 'Correct totals', kind: 'constraint', digest: 'b'.repeat(64), state: 'unreviewed', evidence: null }],
  outputs: [{ id: 'answer', label: 'Answer', kind: 'answer', artifactId: null, version: 'a'.repeat(64), available: true, covered: false },
    { id: 'report', label: 'Report', kind: 'artifact', artifactId: 'file1', version: 'c'.repeat(64), available: true, covered: false }],
  status: 'pending', basis: 'owner_attestation' };
const text = (_key: string, fallback: string) => fallback;
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
function click(host: HTMLElement, label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent === label);
  if (!button) throw new Error('Missing control: ' + label);
  button.click();
}
function input(host: HTMLElement, value: string) {
  const field = host.querySelector<HTMLInputElement>('input:not([type=checkbox])')!;
  field.value = value; field.dispatchEvent(new Event('input'));
}

it('keeps exact selected output versions and notes on conflict/reload until the owner reselects', async () => {
  const host = document.createElement('div');
  const next = { ...value, outputs: value.outputs.map(row => ({ ...row, version: 'd'.repeat(64) })) };
  const load = vi.fn().mockResolvedValueOnce(value).mockResolvedValueOnce(next);
  const save = vi.fn(async () => { throw new Error('409: Output changed'); });
  const open = vi.fn();
  const dispose = mountTurnReview(host, { text, load, save, outputs: { load: async () => [], open } });
  await settle(); expect(host.textContent).toContain('Task execution has not completed');
  input(host, 'Checked the totals'); click(host, 'I verified this'); expect(save).not.toHaveBeenCalled();
  click(host, 'Select all current output versions'); click(host, 'I verified this'); await settle();
  const oldBindings = value.outputs.map(({ id, version }) => ({ id, version }));
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ outputBindings: oldBindings }), expect.any(AbortSignal));
  click(host, 'Reload latest review'); await settle();
  expect(host.textContent).toContain('Earlier version remains selected');
  expect(host.querySelector<HTMLInputElement>('input:not([type=checkbox])')?.value).toBe('Checked the totals');
  click(host, 'I verified this'); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ outputBindings: oldBindings }), expect.any(AbortSignal));
  click(host, 'Select all current output versions'); click(host, 'I verified this'); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ outputBindings: next.outputs.map(({ id, version }) => ({ id, version })) }), expect.any(AbortSignal));
  click(host, 'Open output in a new tab'); expect(open).toHaveBeenCalledWith('file1');
  dispose(); expect(host.childElementCount).toBe(0);
});

it('preserves requirement and output drafts on reload and replaces a version without replacing its slot', async () => {
  const host = document.createElement('div');
  const load = vi.fn().mockResolvedValueOnce(value).mockResolvedValueOnce({ ...value, requirementsRevision: 2 });
  const save = vi.fn(async () => { throw new Error('409'); });
  const dispose = mountTurnReview(host, { text, load, save,
    outputs: { load: async () => [{ id: 'file2', title: '<script>New report</script>' }], open: vi.fn() } });
  await settle(); click(host, 'Edit objective and criteria'); input(host, 'My new objective');
  click(host, 'Choose conversation outputs'); await settle();
  expect(host.querySelector('script')).toBeNull();
  click(host, 'Use selected version for this output'); click(host, 'Save requirements'); await settle();
  click(host, 'Reload latest review'); await settle();
  expect(host.querySelector('input')?.value).toBe('My new objective');
  click(host, 'Save requirements'); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ requirementsRevision: 2,
    objective: 'My new objective', outputs: [{ id: 'report', label: 'Report', artifactId: 'file2' }] }), expect.any(AbortSignal));
  dispose();
});

it('aborts the output picker and excludes its late result when the dialog is retired', async () => {
  const host = document.createElement('div');
  let resolve!: (value: Array<{ id: string; title: string }>) => void;
  const load = vi.fn((_signal: AbortSignal) => new Promise<Array<{ id: string; title: string }>>(done => { resolve = done; }));
  const dispose = mountTurnReview(host, { text, load: async () => value, save: async () => value, outputs: { load, open: vi.fn() } });
  await settle(); click(host, 'Edit objective and criteria'); click(host, 'Choose conversation outputs');
  dispose(); resolve([{ id: 'late', title: 'Late result' }]); await settle();
  expect(load.mock.calls[0][0].aborted).toBe(true); expect(host.childElementCount).toBe(0);
});
