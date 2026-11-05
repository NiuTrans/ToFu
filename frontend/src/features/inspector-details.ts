/** Request Inspector detail reads publish only to their original selected pane.
 * Entry: createInspectorDetails. Session owns view identity, payload owner selects evidence;
 * retained ports supply markup and the common Debug renderer. No second message renderer.
 */
import type {InspectorPane, InspectorSession} from './inspector-session';
import type {InspectorPayload} from './inspector-payloads';

interface Ports {
  trace(task: string): Promise<unknown>;
  round(task: string, round: number, turn: string): Promise<{kind: string; payload: InspectorPayload} | null>;
  loading(kind: 'trace' | 'detached'): void;
  showTrace(data: unknown): void;
  messages(payload: InspectorPayload): boolean;
  back(pane: InspectorPane): void;
  failed(retry: () => void): void;
  feed(): void;
  land(task: string, round: number): Promise<void>;
  log(error: unknown): void;
}
class InspectorDetails {
  constructor(private readonly session: InspectorSession, private readonly ports: Ports) {}
  async trace(task: string): Promise<void> {
    if (!task) return;
    const pane = this.session.selectPane('trace');
    if (!pane) return;
    this.ports.loading('trace');
    this.ports.back(pane);
    let data: unknown = null;
    try { data = await this.ports.trace(task); }
    catch (error) { this.ports.log(error); }
    if (!pane.isCurrent()) return;
    this.ports.showTrace(data);
    this.ports.back(pane);
  }
  async detached(task: string, round: number, turn: string): Promise<void> {
    const pane = this.session.selectPane('detached');
    if (!pane) return;
    this.ports.loading('detached');
    this.ports.back(pane);
    let view: {kind: string; payload: InspectorPayload} | null;
    try { view = await this.ports.round(task, round, turn); }
    catch (error) {
      this.ports.log(error);
      if (pane.isCurrent()) {
        this.ports.failed(() => { if (pane.isCurrent()) void this.detached(task, round, turn); });
        this.ports.back(pane);
      }
      return;
    }
    if (!pane.isCurrent()) return;
    if (view?.payload?.messages && this.ports.messages(view.payload)) {
      this.ports.back(pane);
      return;
    }
    this.session.showFeed();
    this.ports.feed();
    if (!turn) await this.ports.land(task, round);
  }
}

export function createInspectorDetails(session: InspectorSession, ports: Ports): InspectorDetails {
  return new InspectorDetails(session, ports);
}
