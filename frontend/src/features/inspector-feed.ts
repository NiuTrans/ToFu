/** One flat-feed presentation for the selected Inspector opening.
 * Entry: createInspectorFeed. Session owns pane identity; diagnostics-wire-controller
 * owns read coalescing/last-good evidence; feed-view and pane-view own DOM. Success,
 * loading and failure share the same publication gate. No timers, listeners or DOM cache.
 */
import {createDiagnosticsWireController, type DiagnosticsWireView} from './diagnostics-wire-controller';
import type {InspectorSession} from './inspector-session';
import {renderInspectorFeed, type InspectorFeedDocument, type InspectorFeedViewPorts} from './inspector-feed-view';
import {renderInspectorLoading, renderInspectorFailure, type InspectorContent} from './inspector-pane-view';

interface Ports extends InspectorFeedViewPorts {
  read(conversation: string): Promise<unknown>;
  content(): InspectorContent | null; title(): HTMLElement | null; scroller(): HTMLElement | null;
  navigation: {update(): void; rendered(): void};
  showTurn(turn: string): void; trace(task: string): void;
}
class InspectorFeed {
  private view: DiagnosticsWireView | null = null;
  private presentation: object = {};
  private readonly wire: ReturnType<typeof createDiagnosticsWireController>;
  constructor(private readonly session: InspectorSession, private readonly ports: Ports) {
    this.wire = createDiagnosticsWireController(ports.read);
  }
  private get document(): InspectorFeedDocument | null { return (this.view?.wire as InspectorFeedDocument | null | undefined) ?? null; }
  get turns() { return this.document?.turns || []; }
  get live(): boolean { return this.turns.some(row => row.live); }
  task(task: string) { return this.turns.find(row => row.taskId === task) || null; }
  select(conversation: string | null): void {
    this.view = null; this.presentation = {}; this.wire.select(conversation);
  }
  private ownsPane(): boolean { return this.session.isOpen && this.session.paneKind === 'feed'; }
  async load(conversation: string | null, silent = false): Promise<void> {
    if (!this.session.isOpen || conversation !== this.session.conversationId) return;
    if (!silent && !this.document) { this.view = null; this.render(); }
    const view = await this.wire.load();
    if (!view || !this.session.isOpen || conversation !== this.session.conversationId) return;
    this.view = view; this.render(true);
  }
  render(keepScroll = false): void {
    if (!this.ownsPane()) return;
    this.ports.navigation.update();
    const content = this.ports.content(); if (!content) return;
    const title = this.ports.title(), t = this.ports.translate;
    const presentation = this.presentation = {};
    const current = () => this.presentation === presentation && this.ownsPane()
      && content.isConnected && this.ports.content() === content;
    const retry = () => { if (current()) void this.load(this.session.conversationId); };
    const wire = this.document;
    if (!wire) {
      if (title) title.textContent = t(this.view?.readError ? 'ri.loadFailed' : 'ri.loading');
      if (this.view?.readError) {
        renderInspectorFailure(content, t, retry, 'ri-main-empty');
        content._rawMessages = [];
      } else renderInspectorLoading(content, t);
      return;
    } else {
      renderInspectorFeed(content, title, this.ports.scroller(), wire, !!this.view?.readError, keepScroll, this.ports,
        {retry, showTurn: turn => { if (current()) this.ports.showTurn(turn); }, trace: task => { if (current()) this.ports.trace(task); }});
    }
    this.ports.navigation.rendered();
  }
}

export function createInspectorFeed(session: InspectorSession, ports: Ports): InspectorFeed {
  return new InspectorFeed(session, ports);
}
