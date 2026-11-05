/** Unsaved Provider credential inputs and their submission lifetime.
 * Routing commands own persistence. This view coalesces Enter/blur, releases
 * plaintext/DOM/listeners on retirement and lets an accepted write finish.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';

interface EditorPorts {
  document(): object | null;
  save(providerId: string, secret: string): Promise<boolean>;
  refresh(): void;
}
interface EditorEntry {
  providerId: string;
  document: object | null;
  card: HTMLElement | null;
  input: HTMLInputElement | null;
  scope: LifecycleScope | null;
  pending: Promise<void> | null;
  focusRevision: number;
  editRevision: number;
  failedEditRevision: number | null;
}

class ProviderKeyEditor {
  private readonly entries = new Map<HTMLInputElement, EditorEntry>();
  private refreshPending = false;

  constructor(private readonly ports: EditorPorts) {}

  mount(providerId: string, card: HTMLElement): void {
    const input = card.querySelector<HTMLInputElement>('input');
    const cancel = card.querySelector<HTMLButtonElement>('.stg-keys-btn');
    const document = this.ports.document();
    if (!input || !cancel || !document) return;
    const ownerWindow = card.ownerDocument.defaultView!;
    const scope = createLifecycleScope(ownerWindow);
    const entry: EditorEntry = {providerId, document, card, input, scope,
      pending: null, focusRevision: 0, editRevision: 0, failedEditRevision: null};
    this.entries.set(input, entry);
    scope.listen(card.ownerDocument, 'focusin', () => { entry.focusRevision++; });
    const cancelEntry = () => { this.retire(entry, true); this.refreshIfReady(); };
    scope.listen(cancel, 'click', cancelEntry);
    scope.listen(cancel, 'pointerdown', event => { event.preventDefault(); cancelEntry(); });
    scope.listen(input, 'keydown', event => {
      if ((event as KeyboardEvent).key !== 'Enter') return;
      event.preventDefault();
      void this.submit(input);
    });
    scope.listen(input, 'input', () => { entry.editRevision++; });
    scope.listen(input, 'blur', event => {
      if ((event as FocusEvent).relatedTarget === cancel) return;
      const next = (event as FocusEvent).relatedTarget as Element | null;
      const dialogSelector = '[role="dialog"], [role="alertdialog"]';
      const nextDialog = next?.closest?.(dialogSelector);
      if (nextDialog && nextDialog !== input.closest(dialogSelector)) return;
      // Lazy error dialogs can focus after this submission has already failed.
      // Unchanged failed text needs an explicit retry; its blur is not one.
      if (entry.failedEditRevision === entry.editRevision) return;
      if (input.value.trim()) void this.submit(input);
    });
    const observer = new ownerWindow.MutationObserver(() => {
      if (!entry.card?.isConnected) this.retire(entry);
    });
    observer.observe(card.ownerDocument.body, {childList: true, subtree: true});
    scope.add(() => observer.disconnect());
    input.focus();
  }

  submit(input: HTMLInputElement): Promise<void> {
    const entry = this.entries.get(input);
    if (!entry || !this.active(entry)) return Promise.resolve();
    if (entry.pending) return entry.pending;
    const secret = input.value.trim();
    if (!secret) { input.focus(); return Promise.resolve(); }
    const focusRevision = entry.focusRevision;
    const editRevision = entry.editRevision;
    entry.failedEditRevision = null;
    // Install the promise before disabling: disabling a focused input may
    // synchronously blur it. That event must join this exact submission.
    let complete!: () => void;
    const pending = new Promise<void>(resolve => { complete = resolve; });
    entry.pending = pending;
    input.disabled = true;
    void this.save(entry, secret, focusRevision, editRevision).then(complete, complete);
    return pending;
  }

  dispose(): void {
    this.refreshPending = false;
    for (const entry of this.entries.values()) this.retire(entry, true);
  }

  private active(entry: EditorEntry): boolean {
    if (!entry.input) return false;
    if (entry.document !== this.ports.document() || !entry.card?.isConnected) {
      this.retire(entry);
      return false;
    }
    return true;
  }

  private async save(entry: EditorEntry, secret: string, focusRevision: number, editRevision: number): Promise<void> {
    const origin = entry.document;
    try {
      const refreshed = await this.ports.save(entry.providerId, secret);
      if (refreshed) {
        // Only this acknowledged write's own adoption may carry newer inputs
        // forward. Arbitrary document replacement still retires their scope.
        for (const current of this.entries.values()) {
          if (current.document === origin) current.document = this.ports.document();
        }
        this.refreshPending = true;
      }
      const current = !!entry.card?.isConnected;
      this.retire(entry, true);
      if (current) this.refreshIfReady();
    } catch {
      // The command adapter presents the failure. Only a still-current input
      // becomes retryable; no detached DOM is held across the await.
      if (!this.active(entry)) return;
      entry.failedEditRevision = editRevision;
      const input = entry.input!;
      input.disabled = false;
      if (entry.focusRevision === focusRevision &&
          input.ownerDocument.activeElement === input.ownerDocument.body) input.focus();
    } finally {
      entry.pending = null;
    }
  }

  private refreshIfReady(): void {
    if (!this.refreshPending || this.entries.size) return;
    this.refreshPending = false;
    this.ports.refresh();
  }

  private retire(entry: EditorEntry, remove = false): void {
    const input = entry.input, card = entry.card, scope = entry.scope;
    entry.input = null;
    entry.card = null;
    entry.document = null;
    entry.scope = null;
    scope?.destroy();
    if (input) {
      this.entries.delete(input);
      input.value = '';
      input.disabled = false;
    }
    if (remove) card?.remove();
  }
}

export function createProviderKeyEditor(ports: EditorPorts): ProviderKeyEditor {
  return new ProviderKeyEditor(ports);
}
