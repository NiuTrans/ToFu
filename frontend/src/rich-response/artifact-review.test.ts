// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { mountArtifactReview } from './artifact-review';
import type { ArtifactReviewView } from '../api/artifact-review.generated';
import type { WorkingFactsView } from '../api/working-facts.generated';

const value: ArtifactReviewView = { schemaVersion: 1, artifactId: 'version1', artifactDigest: 'a'.repeat(64),
  lineageId: 'version1', requirementsRevision: 1, decisionsRevision: 0, objective: 'Readable report',
  criteria: [{ id: 'correct', text: '<script>Correct totals</script>', kind: 'requirement', digest: 'b'.repeat(64),
    state: 'unreviewed', evidence: null }], status: 'pending', basis: 'owner_attestation' };
const text = (_key: string, fallback: string) => fallback;
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const click = (host: HTMLElement, label: string) => {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent === label);
  if (!button) throw new Error('Missing control: ' + label);
  button.click();
};

it('reads lazily, requires evidence, and binds every decision to the displayed version', async () => {
  const host = document.createElement('div');
  const load = vi.fn(async () => value); const save = vi.fn(async () => value);
  const dispose = mountArtifactReview(host, { text, load, save });
  expect(load).not.toHaveBeenCalled();
  click(host, 'Acceptance review'); await settle();
  expect(host.querySelector('script')).toBeNull();
  expect(host.textContent).toContain('<script>Correct totals</script>');
  click(host, 'I verified this'); expect(save).not.toHaveBeenCalled();
  const input = host.querySelector('input')!; input.value = 'Checked the source ledger'; input.dispatchEvent(new Event('input'));
  click(host, 'I verified this'); await settle();
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ artifactDigest: value.artifactDigest,
    criterionDigest: value.criteria[0].digest, requirementsRevision: 1, decisionsRevision: 0,
    note: 'Checked the source ledger', verdict: 'pass' }), expect.any(AbortSignal));
  dispose(); expect(host.childElementCount).toBe(0);
});

it('keeps the draft on conflict and aborts/fences late replies on version switch', async () => {
  const host = document.createElement('div');
  const save = vi.fn(async () => { throw new Error('409: Review changed'); });
  let resolve!: (view: ArtifactReviewView) => void;
  const load = vi.fn().mockResolvedValueOnce(value).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const dispose = mountArtifactReview(host, { text, load, save });
  click(host, 'Acceptance review'); await settle(); click(host, 'Edit objective and criteria');
  const input = host.querySelector('input')!; input.value = 'My unsaved objective'; input.dispatchEvent(new Event('input'));
  click(host, 'Save requirements'); await settle();
  expect(host.querySelector('input')?.value).toBe('My unsaved objective');
  expect(host.querySelector('[role=alert]')?.textContent).toContain('409');
  click(host, 'Reload latest review'); dispose(); resolve(value); await settle();
  expect(load.mock.calls[1][0].aborted).toBe(true); expect(host.childElementCount).toBe(0);
});

const facts = (revision = 1): WorkingFactsView => ({ schemaVersion: 1, conversationId: 'conv', revision,
  semantics: 'source_binding_and_owner_assessment_not_automatic_truth', facts: [{
    id: 'fact', revision, text: revision === 1 ? '<script>Original total</script>' : 'Corrected total',
    kind: 'observation', sources: [], active: true, actor: 'agent', subject: 'agent', producerTaskId: 'task',
    note: '', createdAt: 1, updatedAt: 1, status: 'observed', sourceStates: [], reasons: [],
  }] });

it('loads facts only on demand and preserves the selected version across refresh and conflicts', async () => {
  const host = document.createElement('div');
  const loadFacts = vi.fn().mockResolvedValueOnce(facts()).mockResolvedValueOnce(facts(2));
  const save = vi.fn(async () => { throw new Error('409: Supporting fact changed'); });
  const dispose = mountArtifactReview(host, { text, load: async () => value, save, loadFacts });
  click(host, 'Acceptance review'); await settle();
  expect(loadFacts).not.toHaveBeenCalled();
  click(host, 'Choose supporting facts'); await settle();
  click(host, 'Use this fact version');
  expect(host.querySelector('script')).toBeNull();
  click(host, 'Refresh available facts'); await settle();
  const note = host.querySelector<HTMLInputElement>('input')!;
  note.value = 'Checked original version'; note.dispatchEvent(new Event('input'));
  click(host, 'I verified this'); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ factBindings: [{ id: 'fact', revision: 1 }] }), expect.any(AbortSignal));
  expect(host.querySelector('input')?.value).toBe('Checked original version');
  click(host, 'Use this fact version'); click(host, 'I verified this'); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ factBindings: [{ id: 'fact', revision: 2 }] }), expect.any(AbortSignal));
  dispose();
});

it('keeps old evidence text visible when a source changes and fences an unfinished picker request', async () => {
  const host = document.createElement('div');
  const stale: ArtifactReviewView = { ...value, criteria: [{ ...value.criteria[0], state: 'stale',
    evidence: { artifactId: value.artifactId, artifactDigest: value.artifactDigest, criterionDigest: value.criteria[0].digest,
      verdict: 'pass', note: 'Checked', reviewer: 'owner', reviewedAt: 1,
      factBindings: [{ id: 'fact', revision: 1, text: 'Original total', statusAtReview: 'observed' }] },
    factDependencies: [{ id: 'fact', boundRevision: 1, currentRevision: 2, status: 'changed', reasons: ['fact_revision_changed'] }] }] };
  let resolve!: (result: WorkingFactsView) => void;
  const loadFacts = vi.fn((_signal: AbortSignal) => new Promise<WorkingFactsView>(done => { resolve = done; }));
  const save = vi.fn(async () => stale);
  const dispose = mountArtifactReview(host, { text, load: async () => stale, save, loadFacts });
  click(host, 'Acceptance review'); await settle();
  expect(host.textContent).toContain('Original total');
  expect(host.textContent).toContain('Supporting fact or source changed');
  click(host, 'Choose supporting facts'); dispose(); resolve(facts(2)); await settle();
  expect(loadFacts.mock.calls[0][0].aborted).toBe(true);
  expect(host.childElementCount).toBe(0);
});
