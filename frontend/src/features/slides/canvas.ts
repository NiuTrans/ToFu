/** One live iframe document: owned selection style and listeners, released on page/close. */
import type { EditorView } from './view';

export function createCanvasBinding(view: EditorView, select: (id: string) => void, escape: () => void) {
  let mounted: Document | null = null;
  let cleanup: (() => void) | null = null;
  function clear(): void { cleanup?.(); cleanup = null; mounted = null; }
  function arm(): void {
    clear();
    let doc: Document | null;
    try { doc = view.frame.contentDocument; } catch { return; /* Cross-origin slide previews cannot expose editable DOM. */ }
    // A queued load event from the previous navigation cannot arm an old document.
    if (!doc || doc.URL !== view.frame.src) return;
    mounted = doc;
    const style = doc.createElement('style');
    const ring = view.overlay.ownerDocument.defaultView?.getComputedStyle(view.overlay).getPropertyValue('--focus-ring') ?? '';
    style.textContent = `.sled-selected{box-shadow:${ring || 'none'}}[data-element-id]{cursor:pointer}`;
    doc.head.append(style);
    const click = (event: Event) => {
      const target = event.target as Element | null;
      const hit = target?.closest?.('[data-element-id]');
      if (!hit || doc !== mounted) return;
      event.preventDefault(); event.stopPropagation(); select(hit.getAttribute('data-element-id') ?? '');
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); escape(); }
    };
    doc.addEventListener('click', click); doc.addEventListener('keydown', key);
    cleanup = () => {
      doc.removeEventListener('click', click); doc.removeEventListener('keydown', key);
      doc.querySelectorAll('.sled-selected').forEach(el => el.classList.remove('sled-selected')); style.remove();
    };
  }
  view.frame.addEventListener('load', arm);
  return {
    clear,
    mark(id: string): void {
      mounted?.querySelectorAll('[data-element-id]').forEach(el => el.classList.toggle('sled-selected', el.getAttribute('data-element-id') === id));
    },
    dispose(): void { view.frame.removeEventListener('load', arm); clear(); },
  };
}
