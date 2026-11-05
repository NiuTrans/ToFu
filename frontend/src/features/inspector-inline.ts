/** One inline tool Inspector mount and its original-read publication rights.
 * Entry: createInspectorInline. Exact task/round/turn DOM lookup never interpolates
 * identifiers into selectors. Existing payload/evidence owners perform reads/diffs;
 * the view delegates messages to the common Debug renderer. No poll or DOM observer.
 */
import type {InspectorRef} from './inspector-evidence';
import {inspectorTailMessages, type InspectorPayload} from './inspector-payloads';
import {createInspectorInlinePanel, setInspectorInlineStatus, renderInspectorInlineEvidence,
  type InlineTranslate, type InlineMessages, type InlineMessageRenderer} from './inspector-inline-view';

interface Ports extends InlineMessageRenderer {
  round(task: string, round: number, turn: string): Promise<{kind: string; payload: InspectorPayload} | null>;
  increment(task: string, round: number, kind: string, messages: InlineMessages, turn: string): Promise<InlineMessages | null>;
  detached(task: string, round: number, turn: string): Promise<void>;
  translate: InlineTranslate;
  failed(error: unknown): void;
}
interface MountedPanel {
  ref: InspectorRef;
  panel: WeakRef<HTMLElement>;
  origin: WeakRef<HTMLElement>;
  read: object | null;
}

function resolveTarget(taskId: string, roundNum: number, anchor?: HTMLElement | null): {
  ref: InspectorRef; slot: HTMLElement | null; origin: HTMLElement | null;
} {
  const key = taskId + ':' + roundNum;
  const supplied = anchor?.closest<HTMLElement>('[data-ri-state]');
  // The public entry also accepts an unstamped positional anchor. Its explicit
  // arguments and phase remain valid; a stamped, mismatched entry cannot win.
  let provided = anchor ?? null;
  if (supplied) provided = supplied.dataset.riState === key ? supplied : null;
  const turn = provided?.dataset.riTurn || '';
  const ref = {taskId, roundNum, turn};
  const slot = provided?.closest<HTMLElement>('[data-prn]');
  if (provided?.isConnected && slot?.isConnected) return {ref, slot, origin: provided};
  const matches = [...document.querySelectorAll<HTMLElement>('[data-ri-state]')].filter(node =>
    node.dataset.riState === key && (!turn || node.dataset.riTurn === turn)
    && node.closest('[data-prn]')?.isConnected);
  // Several tools can share a request. A missing phase is resolvable only if
  // every candidate names the same phase; otherwise the server detail owns it.
  if (!matches.length || new Set(matches.map(node => node.dataset.riTurn || '')).size !== 1) {
    return {ref, slot: null, origin: null};
  }
  const origin = matches[0];
  return {ref: {...ref, turn: origin.dataset.riTurn || ''}, slot: origin.closest<HTMLElement>('[data-prn]'), origin};
}

class InspectorInline {
  private active: MountedPanel | null = null;
  constructor(private readonly ports: Ports) {}

  open(taskId: string, roundNum: number, anchor?: HTMLElement | null): Promise<void> {
    if (!taskId || roundNum == null) return Promise.resolve();
    const target = resolveTarget(taskId, roundNum, anchor);
    if (!target.slot || !target.origin) {
      this.close(false);
      return this.ports.detached(taskId, roundNum, target.ref.turn);
    }
    const previous = this.active;
    if (previous && this.node(previous) && previous.ref.taskId === taskId
      && previous.ref.roundNum === roundNum && previous.ref.turn === target.ref.turn) {
      this.close();
      return Promise.resolve();
    }
    this.close(false);
    const panel = createInspectorInlinePanel(target.ref, this.ports.translate, () => {
      if (this.active === mounted) this.close();
    });
    const mounted: MountedPanel = {ref: target.ref, panel: new WeakRef(panel), origin: new WeakRef(target.origin), read: null};
    this.active = mounted;
    target.slot.insertAdjacentElement('afterend', panel);
    panel.scrollIntoView?.({block: 'nearest'});
    // This synchronous frame returns before the read suspends. Pending work
    // carries only weak DOM witnesses, including removal by a chat re-render.
    void this.load(mounted);
    return Promise.resolve();
  }

  close(restoreFocus = true): void {
    const mounted = this.active;
    this.active = null;
    if (!mounted) return;
    mounted.read = null;
    mounted.panel.deref()?.remove();
    const origin = restoreFocus ? mounted.origin.deref() : null;
    if (origin?.isConnected) origin.focus();
  }

  private node(mounted: MountedPanel): HTMLElement | null {
    const node = this.active === mounted ? mounted.panel.deref() : null;
    return node?.isConnected ? node : null;
  }
  private current(mounted: MountedPanel, read: object): boolean {
    return mounted.read === read && this.node(mounted) !== null;
  }
  private status(mounted: MountedPanel, key: string, retry?: () => void): void {
    const node = this.node(mounted);
    if (node) setInspectorInlineStatus(node, mounted.ref, key, this.ports.translate, retry);
  }
  private publish(mounted: MountedPanel, kind: string, payload: InspectorPayload, shown: InlineMessages): void {
    const node = this.node(mounted);
    if (node) renderInspectorInlineEvidence(node, mounted.ref, kind, payload, shown, this.ports.translate, this.ports);
  }
  private async load(mounted: MountedPanel): Promise<void> {
    const read = {};
    mounted.read = read;
    this.status(mounted, 'ri.loading');
    const {taskId, roundNum, turn} = mounted.ref;
    try {
      const view = await this.ports.round(taskId, roundNum, turn);
      if (!this.current(mounted, read)) return;
      if (!view) { this.status(mounted, 'ri.stateEmpty'); return; }
      const {kind, payload} = view;
      const scoped = typeof payload.foldPrefix === 'number' && payload.foldPrefix > 0
        ? payload.messages.slice(payload.foldPrefix)
        : await this.ports.increment(taskId, roundNum, kind, payload.messages, turn);
      if (!this.current(mounted, read)) return;
      let shown = scoped;
      if (!Array.isArray(shown) || !shown.length) {
        shown = kind === 'state' ? inspectorTailMessages(payload.messages) : payload.messages;
      }
      this.publish(mounted, kind, payload, shown);
    } catch (error) {
      this.ports.failed(error);
      if (this.current(mounted, read)) this.status(mounted, 'ri.loadFailed', () => {
        if (this.current(mounted, read)) void this.load(mounted);
      });
    }
  }
}

export function createInspectorInline(ports: Ports): InspectorInline { return new InspectorInline(ports); }
