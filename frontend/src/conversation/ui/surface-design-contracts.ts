/** Visual ports consumed by keyed reconciliation. Type-only; no CSS, clocks,
 * transport or state authority. The composition chooses one design per mount. */
import type { ConversationTurnViewModel } from '../presentation/conversation-view-model';
import type { ToolPanelEntry } from '../presentation/grouped-turn-layout';
import type { ConversationSurfaceRenderers } from './surface-contracts';
import type { RoundOutline } from './round-outline';

export type SurfaceRoundOutline = Pick<RoundOutline,
  'update' | 'beforeBlock' | 'patchBlock' | 'completeBlock' | 'dispose'>;

export type SurfacePart = 'root' | 'main-lane' | 'branch-lane' | 'branch-turns'
  | 'orphan-lane' | 'turn' | 'block' | 'queue-item' | 'turn-content'
  | 'turn-blocks' | 'context-rail' | 'context-card' | 'context-content';
export type SurfaceHistoryDirection = 'earlier' | 'later';
export type SurfaceHistoryStatus = 'loading' | 'failed';
export type SurfaceTransportNotice = 'recovering' | 'degraded' | 'offline';

export interface ConversationSurfaceDesign {
  readonly renderers: Required<Omit<ConversationSurfaceRenderers, 'disposeBlock'>>;
  createRoundOutline(container: HTMLElement, revealBlock: (blockId: string) => void): SurfaceRoundOutline;
  decorate(node: HTMLElement, part: SurfacePart): void;
  turnShell(node: HTMLElement, turn: ConversationTurnViewModel): void;
  /** Paint a reply's tool panel header; the returned button is the toggle.
   * Engine owns its identity, open state and event delegation. */
  toolPanelHeader(node: HTMLElement, panel: ToolPanelEntry): HTMLButtonElement;
  /** Create the shared scrolling well for a reply's interim prose. It must
   * carry `data-narration-well` and one `data-narration-well-body` child;
   * the engine owns which blocks live in the body. */
  narrationWell(document: Document): HTMLElement;
  /** Paint the well's header row for `count` notes; the returned button is
   * its disclosure toggle. Engine owns open state and event delegation. */
  paintNarrationWell(well: HTMLElement, count: number): HTMLButtonElement;
  transportNotice(node: HTMLElement, state: SurfaceTransportNotice): void;
  /** Engine owns identity, disabled state, paging and event delegation. */
  blockBoundary(node: HTMLElement, direction: SurfaceHistoryDirection, count: number): HTMLButtonElement;
  /** A visible row inside the tool-panel card: how many calls the calls
   * window hides on that side. Engine owns identity and paging. */
  toolPanelBoundary(node: HTMLElement, direction: SurfaceHistoryDirection, count: number): HTMLButtonElement;
  windowBoundary(node: HTMLElement, direction: SurfaceHistoryDirection, count: number,
    status?: SurfaceHistoryStatus): HTMLButtonElement;
  /** Consume image-specific markup; navigation policy remains in the engine. */
  imagePreview(event: Event, target: Element, open?: (source: string) => void): boolean;
  isImagePreviewTarget(target: Element): boolean;
}
