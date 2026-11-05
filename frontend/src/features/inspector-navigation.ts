/** Request Inspector feed navigation and its visible DOM resources.
 * Entry: createInspectorNavigation. Reads canonical wire/Turn projections through
 * ports; owns selection identity, divider indexes, scroll/highlight scheduling.
 * It never loads evidence or controls session/pane identity. Shared lifecycle and
 * inspector-navigation-list own teardown and disclosure behavior respectively.
 */
import {createLifecycleScope, type LifecycleScope} from '../lifecycle';
import {createInspectorNavigationList, type InspectorNavigationEntry} from './inspector-navigation-list';

interface WireTurn {turnId?: string; taskId?: string; preview?: string; createdAt?: number; expired?: boolean; readError?: boolean}
interface Ports {
  element(id: string): HTMLElement | null; scroller(): HTMLElement | null;
  feed(): boolean; rows(): readonly WireTurn[];
  orderedTurns(): readonly {turnId?: string; actor?: string}[];
  translate(key: string, params?: Record<string, number>): string;
  relativeTime(time: number): string; absoluteTime(time: number): string;
  showTurn(id: string): void;
}
interface View {
  scope: LifecycleScope; feedScope: LifecycleScope | null;
  content: HTMLElement | null; scroller: HTMLElement | null;
  nav: HTMLElement | null; previous: HTMLButtonElement | null; next: HTMLButtonElement | null;
  readout: HTMLElement | null; list: ReturnType<typeof createInspectorNavigationList> | null;
  entries: InspectorNavigationEntry[]; ordinals: Map<string, number | null>;
  dividers: Map<string, HTMLElement>; flashes: Map<HTMLElement, number>;
  active: string; pendingTask: string; scrollTimer: number;
}
const keyOf = (row: WireTurn): string => row.turnId ? `turn:${row.turnId}` : `task:${row.taskId || ''}`;

class InspectorNavigation {
  private view: View | null = null;
  constructor(private readonly ports: Ports) {}
  open(): void {
    this.close();
    const el = this.ports.element;
    const view: View = {scope: createLifecycleScope(window), feedScope: null, content: null, scroller: null,
      nav: el('riTurnNav'), previous: el('riTurnPrev') as HTMLButtonElement | null,
      next: el('riTurnNext') as HTMLButtonElement | null, readout: el('riTurnReadout'), list: null,
      entries: [], ordinals: new Map(), dividers: new Map(), flashes: new Map(), active: '', pendingTask: '', scrollTimer: 0};
    this.view = view;
    const anchor = el('riTurnPos'), panel = el('riTurnJump');
    if (anchor && panel) {
      view.list = createInspectorNavigationList({...this.ports, anchor, panel, navigate: key => this.navigate(key)});
      anchor.title = this.ports.translate('ri.jumpToTurn'); anchor.setAttribute('aria-label', anchor.title);
      view.scope.listen(anchor, 'click', () => {
        if (!this.current(view) || !this.ports.feed() || view.entries.length < 2) return;
        if (view.list!.isOpen()) view.list!.close(true);
        else { view.list!.update(view.entries, view.active); view.list!.open(); }
      });
    }
    const step = (delta: number) => {
      if (!this.current(view)) return;
      const entry = view.entries[view.entries.findIndex(entry => entry.key === view.active) + delta];
      if (entry) this.navigate(entry.key);
    };
    if (view.previous) view.scope.listen(view.previous, 'click', () => step(-1));
    if (view.next) view.scope.listen(view.next, 'click', () => step(1));
  }
  private current(view: View): boolean { return this.view === view && !view.scope.signal.aborted; }
  close(): void {
    const view = this.view; this.view = null;
    if (!view) return;
    this.releaseFeed(view); view.scope.destroy(); view.list?.destroy();
    if (view.nav) view.nav.hidden = true;
  }
  private releaseFeed(view: View): void {
    for (const node of view.flashes.keys()) node.classList.remove('ri-flash');
    view.flashes.clear(); view.feedScope?.destroy(); view.feedScope = null;
    view.scrollTimer = 0; view.content = null; view.scroller = null; view.dividers.clear();
  }
  suspend(): void {
    const view = this.view; if (!view) return;
    this.releaseFeed(view); view.list?.close(false);
    if (view.nav) view.nav.hidden = true;
  }
  update(): void {
    const view = this.view; if (!view) return;
    if (!this.ports.feed()) { this.suspend(); return; }
    view.ordinals.clear(); let ordinal = 0;
    try {
      for (const turn of this.ports.orderedTurns()) {
        if (!turn?.turnId) continue;
        if (turn.actor === 'assistant') ordinal++;
        view.ordinals.set(turn.turnId, ordinal || null);
      }
    } catch { /* Unloaded canonical history keeps the established Earlier label. */ }
    view.entries = this.ports.rows().map(row => ({key: keyOf(row), turnId: row.turnId || '', taskId: row.taskId || '',
      ordinal: this.ordinal(row.turnId), preview: row.preview || '', createdAt: row.createdAt || 0,
      expired: !!row.expired, readError: !!row.readError}));
    if (!view.entries.length) this.releaseFeed(view);
    if (!view.entries.some(entry => entry.key === view.active)) view.active = view.entries.at(-1)?.key || '';
    this.paint(view);
  }
  ordinal(turnId?: string): number | null { return this.view?.ordinals.get(turnId || '') ?? null; }
  private paint(view: View): void {
    const index = view.entries.findIndex(entry => entry.key === view.active), active = view.entries[index];
    if (view.nav) view.nav.hidden = view.entries.length < 2 || !this.ports.feed();
    if (view.previous) view.previous.disabled = index <= 0;
    if (view.next) view.next.disabled = index < 0 || index >= view.entries.length - 1;
    if (active && view.readout) view.readout.textContent = active.ordinal
      ? this.ports.translate('ri.turnChip', {n: active.ordinal}) : this.ports.translate('ri.earlierShort');
    if (view.entries.length < 2) view.list?.close(false);
    else if (view.list?.isOpen()) view.list.update(view.entries, view.active);
  }
  rendered(): void {
    const view = this.view; if (!view || !this.ports.feed()) return;
    const content = this.ports.element('debugContent'), scroller = this.ports.scroller();
    if (!content || !scroller) { this.releaseFeed(view); return; }
    if (view.content !== content || view.scroller !== scroller) {
      this.releaseFeed(view); view.content = content; view.scroller = scroller;
      const scope = view.feedScope = createLifecycleScope(scroller.ownerDocument.defaultView!);
      scope.listen(scroller, 'scroll', () => {
        if (!this.current(view) || !this.ports.feed() || view.scrollTimer) return;
        view.scrollTimer = scope.timeout(() => { view.scrollTimer = 0; this.syncScroll(view); }, 120);
      }, {passive: true});
    }
    for (const [node, timer] of view.flashes) { view.feedScope!.cancelTimeout(timer); node.classList.remove('ri-flash'); }
    view.flashes.clear(); view.dividers.clear();
    for (const node of content.querySelectorAll<HTMLElement>('.ri-feed-turn')) {
      const key = keyOf(node.dataset); if (!view.dividers.has(key)) view.dividers.set(key, node);
    }
    this.consumePending(view);
  }
  private syncScroll(view: View): void {
    if (!this.current(view) || !this.ports.feed() || !view.scroller || view.entries.length < 2) return;
    const boundary = view.scroller.getBoundingClientRect().top + view.scroller.clientTop + view.scroller.clientHeight * 0.3;
    let candidate = '';
    for (const entry of view.entries) {
      const node = view.dividers.get(entry.key); if (!node) continue;
      if (!candidate || node.getBoundingClientRect().top <= boundary) candidate = entry.key;
    }
    if (candidate && candidate !== view.active) { view.active = candidate; this.paint(view); }
  }
  private navigate(key: string): void {
    const view = this.view; if (!view || !this.ports.feed()) return;
    const node = view.dividers.get(key); if (!node) return;
    view.active = key; this.paint(view); view.list?.close(true);
    node.scrollIntoView?.({behavior: 'smooth', block: 'start'});
    const scope = view.feedScope; if (!scope) return;
    scope.cancelTimeout(view.flashes.get(node) || 0); node.classList.add('ri-flash');
    view.flashes.set(node, scope.timeout(() => { view.flashes.delete(node); node.classList.remove('ri-flash'); }, 1600));
  }
  land(taskId: string): void {
    const view = this.view; if (!view) return;
    view.pendingTask = taskId; this.consumePending(view);
  }
  private consumePending(view: View): void {
    if (!view.pendingTask || !this.ports.feed()) return;
    const entry = view.entries.find(entry => entry.taskId === view.pendingTask) ?? view.entries.at(-1);
    if (!entry || !view.dividers.has(entry.key)) return;
    view.pendingTask = ''; this.navigate(entry.key);
  }
}

export function createInspectorNavigation(ports: Ports): InspectorNavigation { return new InspectorNavigation(ports); }
