/** Request Inspector opening, pane intent and disposable polling/subscription resources.
 * Entry: createInspectorSession. Wire evidence remains with diagnostics-wire-controller;
 * this owner neither fetches payloads nor renders messages. Cadences retain existing policy.
 */
import {createLifecycleScope, type LifecycleScope} from '../lifecycle';

export interface InspectorPane {isCurrent(): boolean}
type PaneKind = 'trace' | 'detached';
interface Ports {
  conversation(): string | null;
  content(): HTMLElement | null;
  hidden(): boolean;
  live(): boolean;
  load(conversation: string | null): Promise<void>;
  fingerprint(conversation: string | null): string;
  subscribe(conversation: string | null, changed: () => void): (() => void) | null;
  failed(error: unknown): void;
}
interface Opening {
  conversation: string | null;
  scope: LifecycleScope;
  pane: (InspectorPane & {kind: PaneKind; dispose(): void}) | null;
  poll: number;
  refresh: number;
  fingerprint: string;
}
export class InspectorSession {
  private opening: Opening | null = null;
  constructor(private readonly ports: Ports) {}
  get isOpen(): boolean { return this.opening !== null; }
  get conversationId(): string | null { return this.opening?.conversation ?? null; }
  get paneKind(): PaneKind | 'feed' { return this.opening?.pane?.kind ?? 'feed'; }
  private current(view: Opening): boolean {
    return this.opening === view && !view.scope.signal.aborted
      && this.ports.conversation() === view.conversation;
  }
  open(conversation: string | null): void {
    this.close();
    const view: Opening = {conversation, scope: createLifecycleScope(window), pane: null,
      poll: 0, refresh: 0, fingerprint: this.ports.fingerprint(conversation)};
    this.opening = view;
    try {
      const unsubscribe = this.ports.subscribe(conversation, () => this.changed(view));
      if (unsubscribe) view.scope.add(unsubscribe);
    } catch (error) { this.ports.failed(error); }
    this.schedule(view, 15000);
  }
  close(): void {
    const previous = this.opening;
    this.opening = null;
    if (previous) this.retirePane(previous);
    previous?.scope.destroy();
  }
  selectPane(kind: PaneKind): InspectorPane | null {
    const view = this.opening;
    let content = this.ports.content();
    if (!view || !this.current(view) || !content) return null;
    this.retirePane(view);
    const pane = {kind, dispose: () => { content = null; },
      isCurrent: () => content !== null && this.current(view) && view.pane === pane
        && this.ports.content() === content};
    view.pane = pane;
    return pane;
  }
  showFeed(): void { if (this.opening) this.retirePane(this.opening); }
  private retirePane(view: Opening): void { view.pane?.dispose(); view.pane = null; }
  private schedule(view: Opening, delay: number): void {
    if (!this.current(view)) return;
    view.scope.cancelTimeout(view.poll);
    view.poll = view.scope.timeout(() => {
      view.poll = 0;
      if (!this.current(view)) return;
      if (this.ports.hidden()) this.schedule(view, 15000);
      else void this.refresh(view);
    }, delay);
  }
  private changed(view: Opening): void {
    if (!this.current(view)) return;
    const fingerprint = this.ports.fingerprint(view.conversation);
    if (fingerprint === view.fingerprint) return;
    view.fingerprint = fingerprint;
    if (view.refresh) return;
    view.refresh = view.scope.timeout(() => {
      view.refresh = 0;
      if (this.current(view)) void this.refresh(view);
    }, 800);
  }
  private async refresh(view: Opening): Promise<void> {
    try { await this.ports.load(view.conversation); }
    catch (error) { this.ports.failed(error); }
    if (this.current(view)) this.schedule(view, this.ports.live() ? 3000 : 15000);
  }
}

export function createInspectorSession(ports: Ports): InspectorSession { return new InspectorSession(ports); }
