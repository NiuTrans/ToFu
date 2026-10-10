/** Conversation-row activation and action delegation. Mouse press survives rerenders; clipboard feedback is one fenced transient control. */
import type { LifecycleScope } from '../../lifecycle';
export interface SidebarInteractionPorts {
  activate(id: string): void;
  copy(id: string): Promise<unknown>;
  duplicate(id: string, event: MouseEvent): void;
  delete(id: string, event: MouseEvent): void;
  rename(id: string): void;
  reference(id: string, title: string): void;
  assignFolder(id: string, button: HTMLElement): void;
  reportFailure(error: unknown): void;
}
export function installSidebarInteractions(scope: LifecycleScope, ownerWindow: Window, host: HTMLElement, ports: SidebarInteractionPorts): void {
  let pointer: { id: string | null; time: number } = { id: null, time: 0 };
  let copyEpoch = 0, timer = 0;
  let feedback: { button: HTMLElement; html: string; color: string; priority: string } | null = null;
  function clearFeedback() {
    scope.cancelTimeout(timer); timer = 0;
    if (feedback) { feedback.button.innerHTML = feedback.html; feedback.button.style.setProperty('color', feedback.color, feedback.priority); }
    feedback = null;
  }
  scope.add(() => { copyEpoch++; clearFeedback(); });
  function target(event: Event): Element | null {
    const element = event.target as Element | null;
    return element?.nodeType === 1 ? element : null;
  }
  scope.listen(host, 'pointerdown', event => {
    const e = event as PointerEvent, el = target(e);
    if (e.pointerType !== 'mouse' || e.button !== 0 || !el || el.closest('.conv-actions, .conv-action-btn, button, a')) {
      pointer = { id: null, time: 0 }; return;
    }
    const item = el.closest<HTMLElement>('.conv-item'), id = item?.dataset.convId;
    if (!id) return;
    pointer = { id, time: ownerWindow.performance.now() }; ports.activate(id);
  });
  scope.listen(host, 'click', event => {
    const e = event as MouseEvent, el = target(e);
    if (!el) return;
    const copy = el.closest<HTMLElement>('.conv-copy-id');
    if (copy) {
      e.stopPropagation(); const id = copy.dataset.convId;
      if (!id) return;
      const epoch = ++copyEpoch;
      let request: Promise<unknown>;
      try { request = ports.copy(id); } catch (error) { ports.reportFailure(error); return; }
      Promise.resolve(request).then(() => {
        if (scope.signal.aborted || epoch !== copyEpoch || !host.contains(copy)) return;
        clearFeedback(); feedback = { button: copy, html: copy.innerHTML, color: copy.style.color, priority: copy.style.getPropertyPriority('color') };
        copy.textContent = '✓'; copy.style.color = 'var(--success-text)';
        timer = scope.timeout(clearFeedback, 1200);
      }).catch(error => { if (!scope.signal.aborted) ports.reportFailure(error); });
      return;
    }
    const actions: readonly [string, (id: string, button: HTMLElement) => void][] = [
      ['.conv-dup', id => ports.duplicate(id, e)], ['.conv-delete', id => ports.delete(id, e)],
      ['.conv-rename', ports.rename], ['.conv-ref', (id, b) => ports.reference(id, b.dataset.convTitle || 'Untitled')],
      ['.conv-folder-assign', ports.assignFolder],
    ];
    for (const [selector, action] of actions) {
      const button = el.closest<HTMLElement>(selector);
      if (!button) continue;
      e.stopPropagation(); if (button.dataset.convId) action(button.dataset.convId, button); return;
    }
    const id = el.closest<HTMLElement>('.conv-item')?.dataset.convId;
    if (!id) return;
    if (pointer.id === id && ownerWindow.performance.now() - pointer.time < 1000) { pointer = { id: null, time: 0 }; return; }
    ports.activate(id);
  });
}
