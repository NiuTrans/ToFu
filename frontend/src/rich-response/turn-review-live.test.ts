// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { mountTurnReview, type TurnReviewPorts } from './artifact-review';
import type { TurnReviewView } from '../api/artifact-review.generated';

const initial: TurnReviewView = { schemaVersion: 1, conversationId: 'conv', turnId: 'turn',
  requirementsRevision: 1, decisionsRevision: 0, objective: 'Check the delivery', executionStatus: 'running',
  criteria: [{ id: 'answer', text: 'Correct answer', kind: 'requirement', digest: 'b'.repeat(64), state: 'unreviewed', evidence: null }],
  outputs: [{ id: 'answer', label: 'Answer', kind: 'answer', artifactId: null, version: 'a'.repeat(64), available: true, covered: false }],
  status: 'pending', basis: 'owner_attestation' };
const completed: TurnReviewView = { ...initial, executionStatus: 'completed',
  outputs: initial.outputs.map(row => ({ ...row, version: 'c'.repeat(64) })) };
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
function click(host: HTMLElement, label: string) {
  const control = [...host.querySelectorAll('button')].find(node => node.textContent === label);
  if (!control) throw new Error('Missing control: ' + label);
  control.click();
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function mount(load: TurnReviewPorts['load'], save: TurnReviewPorts['save'] = async () => completed) {
  const host = document.createElement('div'); document.body.append(host);
  let notify: (() => void) | undefined;
  const unsubscribe = vi.fn(() => { notify = undefined; });
  const dispose = mountTurnReview(host, { load, save, text: (_key, fallback) => fallback,
    outputs: { load: async () => [], open: vi.fn() },
    subscribeInvalidation(listener) { notify = listener; return unsubscribe; },
  });
  return { host, unsubscribe, notify: () => { expect(notify).toBeTypeOf('function'); notify!(); },
    close() { dispose(); host.remove(); } };
}

it('coalesces committed changes during a read and retires the subscription and late response', async () => {
  const first = deferred<TurnReviewView>(), last = deferred<TurnReviewView>(), late = deferred<TurnReviewView>();
  const load = vi.fn().mockResolvedValueOnce(initial).mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(last.promise).mockReturnValueOnce(late.promise);
  const mounted = mount(load);
  try {
    await settle(); mounted.notify();
    for (let i = 0; i < 100; i++) mounted.notify();
    expect(load).toHaveBeenCalledTimes(2);
    first.resolve(initial); await settle(); expect(load).toHaveBeenCalledTimes(3);
    last.resolve(completed); await settle();
    expect(mounted.host.textContent).not.toContain('Task execution has not completed');
    expect(load).toHaveBeenCalledTimes(3);
    mounted.notify(); expect(load).toHaveBeenCalledTimes(4);
    mounted.close(); expect(mounted.unsubscribe).toHaveBeenCalledOnce();
    expect(load.mock.calls[3][0].aborted).toBe(true);
    late.resolve(initial); await settle(); expect(mounted.host.childElementCount).toBe(0);
  } finally { mounted.close(); }
});

it('does not replace active input or silently upgrade a draft when a live read returns', async () => {
  const result = deferred<TurnReviewView>();
  const load = vi.fn().mockResolvedValueOnce(initial).mockReturnValueOnce(result.promise).mockResolvedValue(completed);
  const mounted = mount(load);
  try {
    await settle(); mounted.notify();
    const field = mounted.host.querySelector<HTMLInputElement>('input:not([type=checkbox])')!;
    field.focus(); field.value = 'Keep my evidence'; field.setSelectionRange(4, 7);
    field.dispatchEvent(new Event('input', { bubbles: true }));
    result.resolve(completed); await settle();
    expect(document.activeElement).toBe(field); expect(field.selectionStart).toBe(4);
    expect(field.value).toBe('Keep my evidence');
    expect(mounted.host.textContent).toContain('Task execution has not completed');
    expect(mounted.host.querySelector('[data-review-updates]')?.textContent).toContain('Reload latest review');
    field.blur(); await settle(); expect(load).toHaveBeenCalledTimes(2);
    click(mounted.host, 'Reload latest review'); await settle();
    expect(mounted.host.textContent).not.toContain('Task execution has not completed');
    expect(mounted.host.querySelector<HTMLInputElement>('input:not([type=checkbox])')?.value).toBe('Keep my evidence');
  } finally { mounted.close(); }
});

it('retains explicit output selection until reload and never rebinds it to a new version', async () => {
  const load = vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(completed);
  const save = vi.fn(async () => completed);
  const mounted = mount(load, save);
  try {
    await settle(); click(mounted.host, 'Select all current output versions'); mounted.notify();
    await settle(); expect(load).toHaveBeenCalledTimes(1);
    click(mounted.host, 'Reload latest review'); await settle();
    expect(mounted.host.textContent).toContain('Earlier version remains selected');
    const field = mounted.host.querySelector<HTMLInputElement>('input:not([type=checkbox])')!;
    field.value = 'Checked old answer'; field.dispatchEvent(new Event('input'));
    click(mounted.host, 'I verified this'); await settle();
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      outputBindings: [{ id: 'answer', version: initial.outputs[0].version }],
    }), expect.any(AbortSignal));
  } finally { mounted.close(); }
});

it('an older background read cannot overwrite the result of an explicit owner decision', async () => {
  const old = deferred<TurnReviewView>();
  const load = vi.fn().mockResolvedValueOnce(initial).mockReturnValueOnce(old.promise);
  const accepted: TurnReviewView = { ...completed, status: 'accepted', decisionsRevision: 1 };
  const mounted = mount(load, async () => accepted);
  try {
    await settle(); mounted.notify(); click(mounted.host, 'Select all current output versions');
    const field = mounted.host.querySelector<HTMLInputElement>('input:not([type=checkbox])')!;
    field.value = 'Checked'; field.dispatchEvent(new Event('input'));
    click(mounted.host, 'I verified this'); await settle();
    expect(mounted.host.textContent).toContain('Accepted by owner');
    old.resolve(initial); await settle();
    expect(mounted.host.textContent).toContain('Accepted by owner');
  } finally { mounted.close(); }
});

it('a pending requirement edit retains its original revision until an explicit reload', async () => {
  const next = { ...completed, requirementsRevision: 2, objective: 'Changed by another owner action' };
  const load = vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(next);
  const save = vi.fn(async () => { throw new Error('409: requirements changed'); });
  const mounted = mount(load, save);
  try {
    await settle(); click(mounted.host, 'Edit objective and criteria');
    const objective = mounted.host.querySelector<HTMLInputElement>('input')!;
    objective.value = 'My draft objective'; objective.dispatchEvent(new Event('input'));
    mounted.notify(); await settle(); expect(load).toHaveBeenCalledTimes(1);
    click(mounted.host, 'Save requirements'); await settle();
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ requirementsRevision: 1,
      objective: 'My draft objective' }), expect.any(AbortSignal));
    expect(mounted.host.querySelector<HTMLInputElement>('input')?.value).toBe('My draft objective');
    click(mounted.host, 'Reload latest review'); await settle();
    expect(load).toHaveBeenCalledTimes(2);
  } finally { mounted.close(); }
});

it.each(['owner', 'commit'])('a failed automatic read is visible and recovers on a new %s action', async retry => {
  const load = vi.fn().mockResolvedValueOnce(initial).mockRejectedValueOnce(new Error('Unavailable'))
    .mockResolvedValueOnce(completed);
  const mounted = mount(load);
  try {
    await settle(); mounted.notify(); await settle();
    expect(mounted.host.querySelector('[role=alert]')?.textContent).toContain('Unavailable');
    expect(mounted.host.querySelector('input')?.disabled).toBe(false);
    await settle(); expect(load).toHaveBeenCalledTimes(2);
    if (retry === 'owner') click(mounted.host, 'Reload latest review'); else mounted.notify();
    await settle();
    expect(load).toHaveBeenCalledTimes(3);
    expect(mounted.host.textContent).not.toContain('Unavailable');
    expect(mounted.host.textContent).not.toContain('Task execution has not completed');
  } finally { mounted.close(); }
});
