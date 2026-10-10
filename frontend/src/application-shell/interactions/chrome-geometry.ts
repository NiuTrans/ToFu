/** Coalesced chat scroll and composer-height projection. Observer failure rolls back its own resource; teardown fences queued deliveries. */
import type { LifecycleScope } from '../../lifecycle';
export interface ChromeGeometryPorts {
  updateActiveTurn(): void;
  updateScrollButton(): void;
  reportFailure(error: unknown): void;
}
export function installChromeGeometry(scope: LifecycleScope, document: Document, ports: ChromeGeometryPorts): void {
  let frame = 0;
  const chat = document.getElementById('chatContainer');
  if (chat) scope.listen(chat, 'scroll', () => {
    if (!frame) frame = scope.frame(() => {
      frame = 0; ports.updateActiveTurn(); ports.updateScrollButton();
    });
  }, { passive: true });
  const area = document.querySelector<HTMLElement>('.input-area');
  const wrapper = document.querySelector<HTMLElement>('.chat-wrapper');
  if (!area || !wrapper) return;
  const previous = wrapper.style.getPropertyValue('--input-area-h');
  const priority = wrapper.style.getPropertyPriority('--input-area-h');
  scope.add(() => { if (previous) wrapper.style.setProperty('--input-area-h', previous, priority); else wrapper.style.removeProperty('--input-area-h'); });
  function publish() {
    if (scope.signal.aborted || !area || !wrapper) return;
    const value = `${area.offsetHeight}px`;
    if (wrapper.style.getPropertyValue('--input-area-h') === value) return;
    wrapper.style.setProperty('--input-area-h', value); ports.updateScrollButton();
  }
  publish();
  const Resize = document.defaultView?.ResizeObserver;
  if (!Resize) return;
  let observer: ResizeObserver | null = null;
  try { observer = new Resize(publish); scope.add(() => observer?.disconnect()); observer.observe(area); }
  catch (error) { observer?.disconnect(); ports.reportFailure(error); }
}
