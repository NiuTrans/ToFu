/** Focus and delayed cleanup for the existing attachment preview shell.
 * Entry points: showAttachmentPreview, closeAttachmentPreview.
 * Dependencies: injected document/window; no alternate viewer or global listener.
 */
type PreviewState = {
  trigger: HTMLElement | null;
  timer: number | null;
  onKeydown: (event: KeyboardEvent) => void;
};
const states = new WeakMap<HTMLElement, PreviewState>();

export function showAttachmentPreview(document: Document, label = 'Attachment preview'): void {
  const modal = document.getElementById('previewModal');
  const body = document.getElementById('previewBody');
  if (!modal || !body) return;
  let state = states.get(modal);
  if (!state) {
    const active = document.activeElement;
    state = {
      trigger: active instanceof HTMLElement && !modal.contains(active) ? active : null,
      timer: null,
      onKeydown(event) {
        if (event.key !== 'Tab') return;
        const controls = Array.from(modal.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), [tabindex="0"]',
        )).filter(control => control.getClientRects().length);
        const first = controls[0], last = controls.at(-1);
        if (!first || !last) return;
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first.focus();
        }
      },
    };
    states.set(modal, state);
  }
  if (!modal.classList.contains('open')) {
    const active = document.activeElement;
    state.trigger = active instanceof HTMLElement && !modal.contains(active) ? active : null;
  }
  if (state.timer !== null) document.defaultView?.clearTimeout(state.timer);
  state.timer = null;
  modal.removeEventListener('keydown', state.onKeydown);
  modal.addEventListener('keydown', state.onKeydown);
  modal.setAttribute('role', 'dialog');

  modal.setAttribute('aria-label', label);
  modal.setAttribute('aria-modal', 'true');
  modal.classList.add('open');
  body.querySelector<HTMLElement>('.preview-close-btn')?.focus();
}

export function closeAttachmentPreview(document: Document): void {
  const modal = document.getElementById('previewModal');
  const body = document.getElementById('previewBody');
  if (!modal || !body) return;
  modal.classList.remove('open');
  const state = states.get(modal);
  if (state) {
    modal.removeEventListener('keydown', state.onKeydown);
    if (state.timer !== null) document.defaultView?.clearTimeout(state.timer);
    if (state.trigger?.isConnected) state.trigger.focus();
    else document.getElementById('userInput')?.focus();
    state.trigger = null;
    state.timer = document.defaultView?.setTimeout(() => {
      // A reopen cancels this timer; this fence also protects other callers.
      if (!modal.classList.contains('open')) body.replaceChildren();
      states.delete(modal);
    }, 300) ?? null;
  } else if (!modal.classList.contains('open')) {
    body.replaceChildren();
  }
}
