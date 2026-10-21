import { disposeToolEffectNotice } from './renderers/tool-effect-notice';
import { disposeGoalRecoveryNotice } from './renderers/goal-recovery-notice';

import { disposeUsageRecoveryNotice } from './renderers/usage-recovery-notice';
/**
 * Compose typed conversation block renderers for ConversationSurface. Owns
 * dispatch and block disposal only; content policy belongs to renderers/.
 */
import { disposeRichMarkdown } from '../../rich-response/markdown';
import { disposeRichResponse } from '../../rich-response/renderer';
import type { ConversationSurfaceRenderers } from './surface-contracts';
import { disposeLazyToolResult } from './lazy-tool-result';
import type { ClassicConversationRendererPorts } from './renderers/ports';
import {
  renderOriginBlock,
  renderContextBlock,
  renderContextRail,
} from './renderers/context-blocks';
import {
  renderProposedPlanBlock,
  renderPlanExecutionBlock,
} from './renderers/plan-blocks';
import { renderCompactionBlock } from './renderers/compaction-block';
import {
  renderTextBlock,
  disposeThinkingBody,
  renderThinkingBlock,
  renderLiveStatusBlock,
} from './renderers/text-blocks';
import {
  renderToolBlock,
  renderProgramBlock,
  renderInjectionBlock,
  disposeToolBody,
} from './renderers/tool-blocks';
import {
  renderActivityEventBlock,
  renderActivityEventGroupBlock,
} from './renderers/activity-block';
import {
  renderSystemNoteBlock,
  renderSystemNoteGroupBlock,
} from './renderers/system-note-blocks';
import {
  renderFileChangesBlock,
  renderProvenanceBlock,
  renderRolledBackBlock,
} from './renderers/change-receipt-blocks';
import {
  renderImageGenerationBlock,
  renderArtifactsBlock,
  renderAttachmentsBlock,
} from './renderers/media-blocks';
import {
  renderAutopilotRunNoticeBlock,
  renderBranchLaneHeader,
  renderQueueStatusBlock,
  renderNativeQueueItem,
} from './renderers/workflow-blocks';
import {
  renderClassicTurnAvatar,
  renderTurnHeader,
  renderTurnActions,
  renderTurnFooter,
} from './renderers/turn-chrome';

export type {
  ClassicConversationMarkdownOptions,
  ClassicConversationRendererPorts,
} from './renderers/ports';

export function createClassicConversationRenderers(
  ports: ClassicConversationRendererPorts,
): Pick<
  ConversationSurfaceRenderers,
  'renderTurnAvatar' | 'renderBlock' | 'renderTurnHeader' | 'renderTurnActions'
    | 'renderTurnFooter' | 'renderTurnContextRail' | 'renderLaneHeader'
    | 'renderQueueItem' | 'disposeBlock'
> {
  return {
    disposeBlock(node) {

      disposeUsageRecoveryNotice(node);
      disposeGoalRecoveryNotice(node);
      disposeToolEffectNotice(node);

      const body = node.querySelector<HTMLElement>('.md-content');
      if (body) disposeRichMarkdown(body);
      node.querySelectorAll<HTMLElement>('[data-tool-rich-response]').forEach(disposeRichResponse);
      disposeLazyToolResult(node);
      disposeToolBody(node);
      disposeThinkingBody(node);
    },
    renderTurnAvatar(node, turn) {
      renderClassicTurnAvatar(node, turn, ports);
    },
    renderBlock(node, block, context) {

      if (block.kind !== 'activity-event') disposeUsageRecoveryNotice(node);
      if (block.kind !== 'autopilot-run-notice' || block.value.reason !== 'worker_lost') {
        disposeGoalRecoveryNotice(node);
      }
      if (block.kind !== 'tool' && block.kind !== 'program') disposeToolEffectNotice(node);
      if (block.kind !== 'tool') {
        disposeLazyToolResult(node);
        disposeToolBody(node);
      }
      if (block.kind !== 'thinking') disposeThinkingBody(node);
      if (block.kind === 'text') {
        renderTextBlock(node, block, ports, context.turn);
      } else if (block.kind === 'thinking') {
        renderThinkingBlock(node, block, ports);
      } else if (block.kind === 'tool') {
        renderToolBlock(node, block, context.turn, ports);
      } else if (block.kind === 'program') {
        renderProgramBlock(node, block, context.turn, ports);
      } else if (block.kind === 'attachments') {
        renderAttachmentsBlock(node, block, ports);
      } else if (block.kind === 'injections') {
        renderInjectionBlock(node, block, context.turn, ports);
      } else if (block.kind === 'system-note') {
        renderSystemNoteBlock(node, block, ports);
      } else if (block.kind === 'system-note-group') {
        renderSystemNoteGroupBlock(node, block, ports);
      } else if (block.kind === 'file-changes') {
        renderFileChangesBlock(node, block, context.turn, ports);
      } else if (block.kind === 'provenance') {
        renderProvenanceBlock(node, block, context.turn, ports);
      } else if (block.kind === 'origin') {
        renderOriginBlock(node, block, ports);
      } else if (block.kind === 'context') {
        renderContextBlock(node, block, ports);
      } else if (block.kind === 'compaction') {
        renderCompactionBlock(node, block, ports);
      } else if (block.kind === 'rolled-back') {
        renderRolledBackBlock(node, block, ports);
      } else if (block.kind === 'image-generation') {
        renderImageGenerationBlock(node, block, ports);
      } else if (block.kind === 'proposed-plan') {
        renderProposedPlanBlock(node, block, ports);
      } else if (block.kind === 'plan-execution') {
        renderPlanExecutionBlock(node, block, ports);
      } else if (block.kind === 'artifacts') {
        renderArtifactsBlock(node, block);
      } else if (block.kind === 'autopilot-run-notice') {
        renderAutopilotRunNoticeBlock(node, block, ports);
      } else if (block.kind === 'activity-event') {
        renderActivityEventBlock(node, block, ports, context.turn);
      } else if (block.kind === 'activity-event-group') {
        renderActivityEventGroupBlock(node, block, ports, context.turn);
      } else if (block.kind === 'queue-status') {
        renderQueueStatusBlock(node, block, ports);
      } else if (block.kind === 'live-status') {
        renderLiveStatusBlock(node, block, ports);
      } else {
        /* Compile-time completeness; safe blank fallback for unknown runtime data. */
        const unknownBlock: never = block;
        void unknownBlock;
        node.textContent = '';
      }
    },
    renderTurnHeader(node, turn) {
      renderTurnHeader(node, turn, ports);
    },
    renderTurnActions(node, turn) {
      renderTurnActions(node, turn, ports);
    },
    renderTurnFooter(node, turn) {
      renderTurnFooter(node, turn, ports);
    },
    renderTurnContextRail(node, block) {
      renderContextRail(node, block, ports);
    },
    renderLaneHeader(node, lane) {
      renderBranchLaneHeader(node, lane, ports);
    },
    renderQueueItem(node, item, _conversationId, context) {
      renderNativeQueueItem(node, item, ports, context);
    },
  };
}
