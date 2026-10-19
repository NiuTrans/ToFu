/** Read-only coordination reference controls. No debug flag, transcript renderer
 * or hidden request: exact identifiers remain selectable if clipboard fails.
 */
import { createButton } from '../ui';
import { t } from '../i18n';

export interface CoordinationReference {
  conversationId?: string;
  assignmentId?: string;
  workId?: string;
  grantRevision?: number;
  resultRevision?: number | null;
}
export function coordinationReferenceText(reference: CoordinationReference): string {
  return Object.entries(reference).filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${key}: ${value}`).join('\n');
}
export function createCoordinationReference(reference: CoordinationReference, key: string, inspect?: (id: string) => void): HTMLElement {
  const root = document.createElement('div'); root.className = 'coordination-reference';
  const fold = document.createElement('details'); fold.dataset.coordinationDisclosure = `reference:${key}`;
  const summary = document.createElement('summary'); summary.textContent = t('projectCoordination.inspectReference');
  summary.dataset.coordinationFocus = `inspect:${key}`;
  const value = document.createElement('pre'); value.textContent = coordinationReferenceText(reference); value.tabIndex = 0;
  fold.append(summary, value);
  if (inspect && reference.conversationId) fold.append(createButton({ label: t('projectCoordination.inspectConversation'),
    size: 'sm', variant: 'ghost', onClick: () => inspect(reference.conversationId!) }));
  const feedback = document.createElement('small'); feedback.setAttribute('role', 'status');
  const copy = createButton({ label: t('projectCoordination.copyReference'), size: 'sm', variant: 'ghost', onClick: async () => {
    copy.disabled = true;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(value.textContent || '');
      feedback.textContent = t('projectCoordination.referenceCopied');
    } catch {
      fold.open = true;
      feedback.textContent = t('projectCoordination.referenceCopyFailed');
    } finally { copy.disabled = false; }
  } });
  copy.dataset.coordinationFocus = `copy:${key}`;
  root.append(copy, fold, feedback); return root;
}

/** Capture only reader state, never form values or durable execution state.
 * Forms remain mounted; refreshed bounded lists restore native folds/focus/scroll.
 */
export function captureCoordinationView(root: HTMLElement): () => void {
  const active = root.ownerDocument.activeElement;
  const focus = active instanceof HTMLElement && root.contains(active) ? active.dataset.coordinationFocus : undefined;
  const disclosures = new Map([...root.querySelectorAll<HTMLDetailsElement>('details[data-coordination-disclosure]')]
    .map(node => [node.dataset.coordinationDisclosure, node.open]));
  const scrolls = [...root.querySelectorAll<HTMLElement>('[data-coordination-scroll]')]
    .map(node => ({ key: node.dataset.coordinationScroll, left: node.scrollLeft, top: node.scrollTop }));
  const rootScroll = [root.scrollLeft, root.scrollTop];
  return () => {
    for (const node of root.querySelectorAll<HTMLDetailsElement>('details[data-coordination-disclosure]')) {
      const open = disclosures.get(node.dataset.coordinationDisclosure);
      if (open !== undefined) node.open = open;
    }
    for (const node of root.querySelectorAll<HTMLElement>('[data-coordination-scroll]')) {
      const scroll = scrolls.find(item => item.key === node.dataset.coordinationScroll);
      if (scroll) { node.scrollLeft = scroll.left; node.scrollTop = scroll.top; }
    }
    if (focus) [...root.querySelectorAll<HTMLElement>('[data-coordination-focus]')]
      .find(node => node.dataset.coordinationFocus === focus)?.focus({ preventScroll: true });
    root.scrollLeft = rootScroll[0]; root.scrollTop = rootScroll[1];
  };
}
