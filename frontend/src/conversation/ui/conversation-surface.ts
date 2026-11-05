/** Public Surface composition. Chooses the default design once per mount;
 * reconciliation and appearance have independent implementation owners. */
import { createConversationSurfaceDesign } from './surface-design';
import { createConversationSurfaceEngine } from './surface-engine';
import type { ConversationSurface, ConversationSurfaceOptions } from './surface-contracts';

export type {
  ConversationIntent,
  ConversationBlockRenderContext,
  ConversationTurnRenderContext,
  ConversationLaneRenderContext,
  ConversationSurfaceRenderers,
  ScrollAnchorPort,
  ConversationWindowOptions,
  ConversationWindowState,
  ConversationViewportOptions,
  ConversationScrollSnapshot,
  ConversationSurfaceOptions,
  ConversationSurface,
  ConversationRetainedTail,
} from './surface-contracts';
export {
  CONVERSATION_DOM_WINDOW_MAX_TURNS,
  CONVERSATION_DOM_WINDOW_BATCH_TURNS,
  CONVERSATION_HISTORY_PAGE_TURNS,
  CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS,
  CONVERSATION_BLOCK_WINDOW_BATCH_BLOCKS,
} from './surface-windowing';
export { createConversationViewportPort } from './conversation-viewport';

/** Construct one lifecycle-bound owner for a conversation container. */
export function createConversationSurface(
  container: HTMLElement,
  options: ConversationSurfaceOptions = {},
): ConversationSurface {
  return createConversationSurfaceEngine(container, options, createConversationSurfaceDesign(options));
}
