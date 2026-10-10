/** Composer autosizing, image paste and lazy log inspection. One frame/timer/request; late results require unchanged text, context and revision. */
import type { LifecycleScope } from '../../lifecycle';
export interface ComposerInputPorts {
  contextIdentity(): string | null;
  pendingCleanOriginalText(): string | null;
  handleImage(file: File): Promise<unknown> | unknown;
  detectLogNoise(text: string): Promise<unknown>;
  showLogClean(result: unknown): void;
  hideLogClean(): void;
  reportFailure(error: unknown): void;
}
export function installComposerInput(scope: LifecycleScope, input: HTMLTextAreaElement, ports: ComposerInputPorts): void {
  let frame = 0, timer = 0, revision = 0, inspecting = false, trailing = false;
  let pasteContext: string | null = null;
  function inspect() {
    timer = 0;
    if (scope.signal.aborted || pasteContext !== ports.contextIdentity()) return;
    if (inspecting) { trailing = true; return; }
    const text = input.value, context = pasteContext, startedRevision = revision;
    inspecting = true;
    Promise.resolve().then(() => {
      if (scope.signal.aborted) return null;
      return ports.detectLogNoise(text);
    }).then(result => {
      if (scope.signal.aborted || revision !== startedRevision || input.value !== text || ports.contextIdentity() !== context) return;
      if (result) ports.showLogClean(result); else ports.hideLogClean();
    }).catch(error => {
      if (!scope.signal.aborted && revision === startedRevision && ports.contextIdentity() === context) ports.reportFailure(error);
    }).finally(() => {
      inspecting = false;
      if (trailing && !scope.signal.aborted) {
        trailing = false;
        if (!timer) timer = scope.timeout(inspect, 0);
      }
    });
  }
  scope.listen(input, 'input', () => {
    revision++;
    if (!frame) frame = scope.frame(() => {
      frame = 0;
      // Avoid the shrink round-trip while overflowing: it can clamp chat scroll.
      if (input.scrollHeight <= input.clientHeight) input.style.height = 'auto';
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    });
    const original = ports.pendingCleanOriginalText();
    if (original && !input.value.includes(original)) ports.hideLogClean();
  });
  scope.listen(input, 'paste', event => {
    const e = event as ClipboardEvent, items = e.clipboardData?.items;
    if (!items) return;
    const images = Array.from(items).filter(item => item.type.startsWith('image/'));
    if (images.length) {
      e.preventDefault();
      void (async () => {
        for (const item of images) {
          if (scope.signal.aborted) return;
          const file = item.getAsFile(); if (file) await ports.handleImage(file);
        }
      })().catch(error => { if (!scope.signal.aborted) ports.reportFailure(error); });
      return;
    }
    const pastedText = e.clipboardData?.getData('text');
    if (!pastedText || pastedText.length <= 200) return;
    revision++; pasteContext = ports.contextIdentity();
    scope.cancelTimeout(timer); timer = scope.timeout(inspect, 50);
  });
}
