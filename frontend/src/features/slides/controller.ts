/** Single editor session and page-visit authority. Requests retain identities, never old DOM.
 * Entry: createSlidesEditor; dependencies: shell/properties, canvas, bounded API reader.
 */
import { createLifecycleScope } from '../../lifecycle';
import type { EditorPorts, PageVisit, SlideElement, SlidesApi } from './contracts';
import { createElementReader } from './element-reader';
import { createEditorView, renderPageChrome, type EditorView } from './view';
import { renderProperties, collectPatch } from './properties';
import { createCanvasBinding } from './canvas';

export function createSlidesEditor(ports: EditorPorts) {
  interface Session {
    taskId: string; total: number; api: SlidesApi; view: EditorView;
    visit: PageVisit | null; elements: SlideElement[]; selected: string;
    scope: ReturnType<typeof createLifecycleScope>; canvas: ReturnType<typeof createCanvasBinding>;
  }
  let session: Session | null = null;
  let writing = false;
  const isCurrent = (visit: PageVisit) => session?.visit === visit;
  const render = () => {
    if (session) renderProperties(session.view.side, session.elements.find(el => el.elementId === session?.selected), ports.translate, writing);
  };
  const reader = createElementReader(isCurrent, (_, elements) => { if (session) { session.elements = elements; render(); } });
  function close(): void {
    const previous = session; session = null; reader.clear();
    if (!previous) return;
    previous.visit = null; previous.elements = []; previous.selected = '';
    previous.scope.destroy();
  }
  function loadPage(page: number): void {
    const current = session;
    if (!current || !Number.isInteger(page) || page < 1 || page > current.total) return;
    current.canvas.clear(); current.selected = ''; current.elements = [];
    const visit = { taskId: current.taskId, page }; current.visit = visit;
    renderPageChrome(current.view, current.api, current.taskId, page, current.total, ports.translate);
    current.view.frame.src = current.api.pageHtmlUrl(current.taskId, page) + '?_=' + Date.now();
    render(); reader.load(current.api, visit);
  }
  function setBusy(): void {
    const button = session?.view.side.querySelector<HTMLButtonElement>('[data-sled-apply]');
    if (button) button.disabled = writing;
  }
  function error(message: string): void {
    const errors = session?.view.side.querySelector('#sledErrors');
    if (errors) errors.textContent = message;
  }
  async function apply(): Promise<void> {
    if (!session?.visit || writing) return;
    const visit = session.visit, api = session.api;
    const element = session.elements.find(el => el.elementId === session?.selected);
    if (!element || element.elementType !== 'text') return;
    const ops = collectPatch(session.view.side, element);
    if (!ops.length) { error(ports.translate('sled.noChange')); return; }
    writing = true; setBusy(); error('');
    try {
      let result;
      try {
        result = await api.patchPage(visit.taskId, visit.page, ops);
      } catch {
        // A rejected patch transport has no success receipt; preserve the form for retry.
        if (isCurrent(visit)) error(ports.translate('sled.failed'));
        return;
      }
      if (!isCurrent(visit)) return;
      if (result?.ok) {
        ports.notifyApplied(); loadPage(visit.page);
      } else {
        error((result?.errors ?? [result?.detail ?? 'failed']).filter(Boolean).join('；').slice(0, 400));
      }
    } finally {
      writing = false; setBusy();
    }
  }
  function ask(): void {
    if (!session?.visit) return;
    const message = ports.translate('sled.askAiPrefill', { id: session.taskId, n: session.visit.page });
    close(); ports.prefill(message);
  }
  function open(task: unknown, requestedPage: unknown, requestedTotal: unknown): void {
    const taskId = String(task ?? '');
    const page = Math.max(1, Math.trunc(Number(requestedPage) || 1));
    const total = Math.max(page, Math.trunc(Number(requestedTotal) || page));
    const api = ports.getApi();
    if (!taskId || !api || !Number.isFinite(page) || !Number.isFinite(total)) return;
    close();
    const view = createEditorView(ports.document, ports.translate), scope = createLifecycleScope();
    const origin = ports.document.activeElement as HTMLElement | null;
    const canvas = createCanvasBinding(view, id => {
      if (!session || session.view !== view) return;
      session.selected = id; canvas.mark(id); render();
    }, close);
    session = { taskId, total, api, view, scope, canvas, visit: null, elements: [], selected: '' };
    scope.add(() => { if (origin?.isConnected) origin.focus(); });
    scope.add(() => view.overlay.remove()); scope.add(canvas.dispose);
    scope.listen(view.overlay, 'click', event => {
      const target = event.target as Element | null;
      const pageButton = target?.closest<HTMLElement>('[data-sled-page]');
      if (pageButton) loadPage(Number(pageButton.dataset.sledPage));
      else if (target?.closest('[data-sled-apply]')) void apply();
      else if (target?.closest('[data-sled-ask]')) ask();
      else if (target?.closest('.sled-close') || target === view.overlay) close();
    });
    scope.listen(ports.document, 'keydown', event => {
      const key = event as KeyboardEvent;
      if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); close(); }
      if (key.key === 'Tab') {
        const controls = [...view.shell.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input,textarea,iframe')];
        const first = controls[0], last = controls[controls.length - 1];
        if (key.shiftKey && ports.document.activeElement === first) { key.preventDefault(); last?.focus(); }
        else if (!key.shiftKey && ports.document.activeElement === last) { key.preventDefault(); first?.focus(); }
      }
    });
    if (ports.document.defaultView) scope.listen(ports.document.defaultView, 'pagehide', close);
    ports.document.body.append(view.overlay); loadPage(page); view.close.focus();
  }
  return { open, close };
}
