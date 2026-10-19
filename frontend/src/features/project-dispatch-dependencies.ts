/** Dependency UI in the existing drawer: local exact pins, backend-only readiness.
 * Refresh never substitutes newer references. No writes, timers or delivery inference.
 */
import { createAlert, createChoice } from '../ui';
import { t } from '../i18n';
import type { ProjectDispatchDependency, ProjectDispatchDependencyState, ProjectDispatchView } from '../api/project-dispatch.generated';
const text = (value: string, tag = 'p') => {
  const node = document.createElement(tag); node.textContent = value; return node;
};

/** Break long identifiers without truncation or changing their text/accessibility value. */
function wrapReference(node: Element) {
  const value = node.textContent || '';
  node.replaceChildren();
  for (const part of value.split(/([A-Za-z0-9_-]{17,})/)) {
    if (/^[A-Za-z0-9_-]{17,}$/.test(part)) {
      const chunks = part.match(/.{1,12}/g) || [];
      chunks.forEach((chunk, index) => {
        if (index) node.append(document.createElement('wbr'));
        node.append(document.createTextNode(chunk));
      });
    } else node.append(document.createTextNode(part));
  }
}
export function sameDependency(a: ProjectDispatchDependency, b: ProjectDispatchDependency): boolean {
  return a.assignmentId === b.assignmentId && a.grantRevision === b.grantRevision
    && a.resultRevision === b.resultRevision && a.checkpointSha === b.checkpointSha;
}
export function sameDependencies(a: ProjectDispatchDependency[] | undefined, b: ProjectDispatchDependency[] | undefined): boolean {
  if (!a || !b) return a === b;
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length
    && a.every((reference, index) => sameDependency(reference, b[index]));
}
function referenceLabel(reference: ProjectDispatchDependency): string {
  return t('projectDispatch.dependencyPin', { assignmentId: reference.assignmentId,
    grantRevision: reference.grantRevision, resultRevision: reference.resultRevision })
    + (reference.checkpointSha ? ` · ${t('projectDispatch.checkpoint', { sha: reference.checkpointSha })}` : '');
}
function candidateReference(view: ProjectDispatchView): ProjectDispatchDependency | null {
  if (view.revoked || view.execution?.status !== 'completed' || !Number.isSafeInteger(view.grantRevision)
    || view.grantRevision < 1 || !Number.isSafeInteger(view.execution.resultRevision) || view.execution.resultRevision! < 1) return null;
  const files = view.request.profile?.mode === 'files';
  if (files && !view.delivery?.checkpointSha) return null;
  return { assignmentId: view.assignmentId, grantRevision: view.grantRevision,
    resultRevision: view.execution.resultRevision!, ...(files ? { checkpointSha: view.delivery!.checkpointSha } : {}) };
}
export function createDispatchDependencies(path: string, canSelect: () => boolean, changed: () => void) {
  const el = document.createElement('section'); el.className = 'ui-stack ui-stack--tight';
  el.dataset.testid = 'dispatch-dependencies';
  let selected: ProjectDispatchDependency[] = [];
  let candidates: ProjectDispatchView[] = [];
  let disabled = false;
  let known = false;
  const samePath = (value: string) => value.replace(/[/\\]+$/, '') === path.replace(/[/\\]+$/, '');
  function render() {

    const active = document.activeElement;
    const focusedId = active instanceof HTMLInputElement && el.contains(active) ? active.dataset.assignmentId : undefined;
    el.replaceChildren(text(t('projectDispatch.dependencies'), 'strong'), text(t('projectDispatch.dependenciesHint'), 'small'),
      text(t('projectDispatch.dependenciesCount', { count: selected.length }), 'small'));
    const selectable = candidates.map(view => ({ view, reference: candidateReference(view) }));
    const add = (reference: ProjectDispatchDependency, stale: boolean) => {
      const checked = selected.some(pin => sameDependency(pin, reference));
      const choice = createChoice({ label: referenceLabel(reference), checked, name: 'dependency',
        disabled: disabled || (!checked && selected.length >= 8) });
      choice.input.dataset.assignmentId = reference.assignmentId;

      wrapReference(choice.el.querySelector('span')!);
      choice.input.addEventListener('change', () => {
        if (disabled || !canSelect()) { choice.input.checked = checked; return; }
        if (choice.input.checked) {
          if (selected.length >= 8 || selected.some(pin => pin.assignmentId === reference.assignmentId)) {
            render(); return;
          }
          selected.push({ ...reference });
        } else selected = selected.filter(pin => !sameDependency(pin, reference));
        changed(); render();
      });
      el.append(choice.el);
      if (stale) el.append(createAlert({ variant: 'warning', title: t('projectDispatch.dependencyStale'),
        body: t('projectDispatch.dependencyStaleHint') }).el);
    };
    for (const pin of selected) add(pin, !known || !selectable.some(row => row.reference && sameDependency(row.reference, pin)));
    for (const { view, reference } of selectable) {
      if (selected.some(pin => pin.assignmentId === view.assignmentId)) continue;
      if (reference) add(reference, false);
      else if (view.request.profile?.mode === 'files' && !view.delivery?.checkpointSha) {
        el.append(createChoice({ label: `${view.assignmentId} · ${t('projectDispatch.dependencyCheckpointRequired')}`,
          checked: false, disabled: true, name: 'dependency-unavailable' }).el);
      }
    }
    if (!known) el.append(text(t('projectDispatch.dependenciesUnavailable')));
    else if (!candidates.length && !selected.length) el.append(text(t('projectDispatch.dependenciesEmpty')));

    if (focusedId) [...el.querySelectorAll<HTMLInputElement>('input')]
      .find(input => input.dataset.assignmentId === focusedId && !input.disabled)?.focus({ preventScroll: true });
  }
  render();
  return {
    el,
    references: () => selected.map(pin => ({ ...pin })),
    restore(pins: ProjectDispatchDependency[] = []) { selected = pins.map(pin => ({ ...pin })); render(); },
    update(views: ProjectDispatchView[] | null) {
      known = views !== null;
      candidates = (views || []).slice(0, 128).filter(view => samePath(view.request.projectPath)
        && !view.revoked && view.execution?.status === 'completed');
      render();
    },
    disable(value: boolean) { if (disabled !== value) { disabled = value; render(); } },
  };
}
const statusKeys = {
  ready: 'projectDispatch.dependencyStatusReady', waiting_review: 'projectDispatch.dependencyStatusWaitingReview',
  waiting_delivery: 'projectDispatch.dependencyStatusWaitingDelivery', invalid: 'projectDispatch.dependencyStatusInvalid',
  unknown: 'projectDispatch.dependencyStatusUnknown',
} as const satisfies Record<ProjectDispatchDependencyState['status'], Parameters<typeof t>[0]>;
const reasonKeys = {
  ready: 'projectDispatch.dependencyReasonReady', review_required: 'projectDispatch.dependencyReasonReviewRequired',
  delivery_required: 'projectDispatch.dependencyReasonDeliveryRequired', revoked: 'projectDispatch.dependencyReasonRevoked',
  scope_changed: 'projectDispatch.dependencyReasonScopeChanged', result_changed: 'projectDispatch.dependencyReasonResultChanged',
  review_rejected: 'projectDispatch.dependencyReasonReviewRejected', stopped: 'projectDispatch.dependencyReasonStopped',
  evidence_unavailable: 'projectDispatch.dependencyReasonEvidenceUnavailable', dependency_changed: 'projectDispatch.dependencyReasonDependencyChanged',
  checkpoint_changed: 'projectDispatch.dependencyReasonCheckpointChanged',
} as const satisfies Record<ProjectDispatchDependencyState['reason'], Parameters<typeof t>[0]>;
export function dependencyProjection(view: ProjectDispatchView): HTMLElement | null {
  if (!view.request.dependencies?.length) return null;
  const el = document.createElement('section'); el.className = 'ui-stack ui-stack--tight';
  el.dataset.testid = 'dependency-states'; el.append(text(t('projectDispatch.dependencyStates'), 'strong'));
  for (const reference of view.request.dependencies) {
    const state = view.dependencyStates?.find(value => sameDependency(value.reference, reference));
    // Missing/mismatched server projections are unknown, never inferred ready.
    const status = state && statusKeys[state.status] || statusKeys.unknown;
    const reason = state && reasonKeys[state.reason] || reasonKeys.evidence_unavailable;
    const label = text(referenceLabel(reference), 'small'); wrapReference(label);
    el.append(label, text(t('projectDispatch.dependencyState', {
      status: t(status), reason: t(reason),
    })));
  }
  return el;
}
