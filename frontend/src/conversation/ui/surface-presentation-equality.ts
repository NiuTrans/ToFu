/**
 * Bounded, DOM-free presentation equality for keyed repaint decisions. Unknown
 * or oversized values force a safe render.
 */
import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';

/**
 * Compare JSON-like presentation values without allocating a serialized copy.
 *
 * Full conversation snapshots legitimately recreate contract objects. Object
 * identity alone would therefore re-run every block renderer even when its
 * visible value was unchanged. The depth and node budgets keep this check a
 * bounded UI optimization; exceeding either budget safely falls back to a
 * render instead of risking an unbounded walk on an unexpected payload.
 */
const STRUCTURED_COMPARE_MAX_DEPTH = 32;

const STRUCTURED_COMPARE_MAX_NODES = 4096;

interface StructuredCompareBudget {
  remaining: number;
}

function sameStructuredValue(
  previous: unknown,
  current: unknown,
  budget: StructuredCompareBudget,
  depth = 0,
): boolean {
  // Charge primitive leaves too: a shallow array can contain millions of
  // values without consuming the old object-only budget.
  if (budget.remaining <= 0) return false;
  budget.remaining -= 1;
  if (Object.is(previous, current)) return true;
  if (previous === null || current === null
      || typeof previous !== 'object' || typeof current !== 'object') {
    return false;
  }
  if (depth >= STRUCTURED_COMPARE_MAX_DEPTH) {
    return false;
  }
  const previousIsArray = Array.isArray(previous);
  const currentIsArray = Array.isArray(current);
  if (previousIsArray !== currentIsArray) return false;
  if (previousIsArray && currentIsArray) {
    if (previous.length !== current.length) return false;
    if (previous.length > budget.remaining) return false;
    for (let index = 0; index < previous.length; index += 1) {
      if ((index in previous) !== (index in current)
          || !sameStructuredValue(previous[index], current[index], budget, depth + 1)) {
        return false;
      }
    }
    return true;
  }
  const previousPrototype = Object.getPrototypeOf(previous);
  const currentPrototype = Object.getPrototypeOf(current);
  if (previousPrototype !== currentPrototype
      || (previousPrototype !== Object.prototype && previousPrototype !== null)) {
    return false;
  }
  const previousRecord = previous as Record<string, unknown>;
  const currentRecord = current as Record<string, unknown>;
  const previousKeys = Object.keys(previousRecord);
  const currentKeys = Object.keys(currentRecord);
  if (previousKeys.length !== currentKeys.length) return false;
  if (previousKeys.length > budget.remaining) return false;
  return previousKeys.every((key) => (
    Object.prototype.hasOwnProperty.call(currentRecord, key)
    && sameStructuredValue(
      previousRecord[key], currentRecord[key], budget, depth + 1,
    )
  ));
}

export function samePresentationValue(previous: unknown, current: unknown): boolean {
  return sameStructuredValue(previous, current, {
    remaining: STRUCTURED_COMPARE_MAX_NODES,
  });
}

export function sameBlock(
  previous: ConversationBlockViewModel | undefined,
  current: ConversationBlockViewModel,
): boolean {
  if (!previous || previous.kind !== current.kind
      || previous.blockId !== current.blockId) return false;
  if (previous.kind === 'text' && current.kind === 'text') {
    return previous.translationModel === current.translationModel
      && previous.inputTranslationCompleted === current.inputTranslationCompleted
      && previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.deliverable === current.deliverable
      && previous.terminal === current.terminal
      && previous.resumable === current.resumable;
  }
  if (previous.kind === 'thinking' && current.kind === 'thinking') {
    return previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.terminal === current.terminal
      && previous.signature === current.signature
      && samePresentationValue(
        previous.memberBlockIds, current.memberBlockIds,
      );
  }
  if (previous.kind === 'proposed-plan' && current.kind === 'proposed-plan') {
    return previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.translationPending === current.translationPending
      && previous.translationStreaming === current.translationStreaming;
  }
  if (previous.kind === 'tool' && current.kind === 'tool') {
    return previous.effectNotice === current.effectNotice
      && previous.toolCallId === current.toolCallId
      && previous.name === current.name
      && samePresentationValue(previous.input, current.input)
      && samePresentationValue(previous.result, current.result)
      && samePresentationValue(previous.round, current.round);
  }
  if (previous.kind === 'program' && current.kind === 'program') {
    return previous.effectNotice === current.effectNotice
      && previous.programCallId === current.programCallId
      && samePresentationValue(previous.round, current.round);
  }
  if (previous.kind === 'attachments' && current.kind === 'attachments') {
    return samePresentationValue(previous.images, current.images)
      && samePresentationValue(
        previous.mediaAttachments, current.mediaAttachments,
      )
      && samePresentationValue(previous.videos, current.videos)
      && samePresentationValue(previous.pdfTexts, current.pdfTexts)
      && samePresentationValue(
        previous.conversationReferences, current.conversationReferences,
      )
      && samePresentationValue(previous.replyQuotes, current.replyQuotes);
  }
  if (previous.kind === 'injections' && current.kind === 'injections') {
    return previous.channel === current.channel
      && previous.anchorLlmRound === current.anchorLlmRound
      && samePresentationValue(previous.items, current.items);
  }
  if (previous.kind === 'system-note' && current.kind === 'system-note') {
    return previous.noteKind === current.noteKind
      && previous.text === current.text
      && previous.action === current.action
      && previous.reason === current.reason
      && previous.injectionId === current.injectionId
      && previous.llmRound === current.llmRound
      && samePresentationValue(previous.provenance, current.provenance);
  }
  if (previous.kind === 'system-note-group'
      && current.kind === 'system-note-group') {
    return samePresentationValue(previous.provenance, current.provenance)
      && previous.items.length === current.items.length
      && previous.items.every((item, index) => {
        const other = current.items[index];
        return other !== undefined && sameBlock(item, other);
      });
  }
  if (previous.kind === 'file-changes' && current.kind === 'file-changes') {
    return previous.count === current.count
      && previous.truncated === current.truncated
      && samePresentationValue(previous.files, current.files)
      && previous.state === current.state
      && previous.commandAvailable === current.commandAvailable
      && samePresentationValue(previous.error, current.error);
  }
  if (previous.kind === 'provenance' && current.kind === 'provenance') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'activity-event'
      && current.kind === 'activity-event') {
    return samePresentationValue(previous.value, current.value)
      && samePresentationValue(previous.terminalError, current.terminalError);
  }
  if (previous.kind === 'autopilot-run-notice'
      && current.kind === 'autopilot-run-notice') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'origin' && current.kind === 'origin') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'context' && current.kind === 'context') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'compaction' && current.kind === 'compaction') {
    return previous.summaryMarkdown === current.summaryMarkdown
      && samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'rolled-back' && current.kind === 'rolled-back') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'image-generation'
      && current.kind === 'image-generation') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'plan-execution'
      && current.kind === 'plan-execution') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'artifacts' && current.kind === 'artifacts') {
    return samePresentationValue(previous.artifacts, current.artifacts);
  }
  if (previous.kind === 'live-status' && current.kind === 'live-status') {
    return samePresentationValue(previous.value, current.value);
  }
  return samePresentationValue(previous.source, current.source);
}

function turnTimestamp(
  turn: ConversationTurnViewModel,
): string | number | undefined {
  return turn.source?.projection?.timestamp ?? turn.source?.createdAt;
}

function sameFinish(
  previous: ConversationTurnViewModel['finish'],
  current: ConversationTurnViewModel['finish'],
): boolean {
  if (previous === current) return true;
  if (!previous || !current) return false;
  return previous.tone === current.tone
    && previous.label === current.label
    && previous.detail === current.detail
    && previous.errorKind === current.errorKind
    && previous.retryable === current.retryable
    && (previous.resumeOptions?.length ?? 0) === (current.resumeOptions?.length ?? 0)
    && (previous.resumeOptions ?? []).every((option, index) => {
      const next = current.resumeOptions?.[index];
      return option.operation === next?.operation && option.anchor === next?.anchor;
    });
}

export function sameTurnHeader(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  return Boolean(previous
    && previous.actor === current.actor
    && previous.role === current.role
    && previous.kind === current.kind
    && previous.status === current.status
    && previous.attemptId === current.attemptId
    && previous.commandPending === current.commandPending
    && Object.is(turnTimestamp(previous), turnTimestamp(current))
    && samePresentationValue(
      previous.metadata.orchestration, current.metadata.orchestration,
    )
    && samePresentationValue(previous.metadata.origin, current.metadata.origin)
    && samePresentationValue(previous.metadata.taskTimePresentations, current.metadata.taskTimePresentations)
    && sameFinish(previous.finish, current.finish));
}

export function sameTurnAvatar(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  return Boolean(previous
    && previous.actor === current.actor
    && previous.role === current.role
    && previous.kind === current.kind
    && samePresentationValue(previous.metadata.origin, current.metadata.origin));
}

export function sameTurnActions(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  const previousActions = previous?.actions ?? [];
  const currentActions = current.actions ?? [];
  if (!previous || previousActions.length !== currentActions.length) return false;
  return previousActions.every((action, index) => {
    const next = currentActions[index];
    return action.action === next?.action
      && action.operation === next?.operation
      && action.disabled === next?.disabled;
  });
}

export function sameTurnFooter(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  const previousFallback = previous?.metadata.fallback;
  const currentFallback = current.metadata.fallback;
  const terminal = ['completed', 'interrupted', 'truncated', 'failed']
    .includes(current.status);
  return Boolean(previous
    && sameFinish(previous.finish, current.finish)
    && previous.status === current.status
    && previous.commandPending === current.commandPending
    && (!terminal || previous.projectionRevision === current.projectionRevision)
    && previous.metadata.model === current.metadata.model
    && previous.metadata.preset === current.metadata.preset
    && previous.metadata.translation.pending === current.metadata.translation.pending
    && previous.metadata.costSignature === current.metadata.costSignature
    && previousFallback?.model === currentFallback?.model
    && previousFallback?.from === currentFallback?.from
    && previousFallback?.reason === currentFallback?.reason
    && previousFallback?.kind === currentFallback?.kind);
}

export function samePlanDecision(
  previous: PlanDecisionViewModel | undefined,
  current: PlanDecisionViewModel,
): boolean {
  return Boolean(previous
    && previous.sourceTurnId === current.sourceTurnId
    && previous.sourceProjectionRevision === current.sourceProjectionRevision
    && previous.planId === current.planId
    && previous.pending === current.pending);
}

export function sameLaneHeader(
  previous: ConversationLaneViewModel | undefined,
  current: ConversationLaneViewModel,
): boolean {
  return Boolean(previous
    && previous.title === current.title
    && previous.label === current.label
    && previous.icon === current.icon
    && previous.kind === current.kind
    && previous.anchorText === current.anchorText
    && previous.parentSelection === current.parentSelection
    && previous.expanded === current.expanded
    && previous.live === current.live
    && previous.humanTurnCount === current.humanTurnCount);
}

export function sameQueueItem(
  previous: ConversationQueueItemViewModel | undefined,
  current: ConversationQueueItemViewModel,
): boolean {
  if (!previous) return false;
  return previous.queueId === current.queueId
    && previous.position === current.position
    && previous.kind === current.kind
    && previous.text === current.text
    && previous.source.priority === current.source.priority
    && previous.source.hasImages === current.source.hasImages
    && previous.source.hasPdfs === current.source.hasPdfs
    && previous.source.hasAttachments === current.source.hasAttachments
    && previous.source.hasRefs === current.source.hasRefs
    && previous.source.hasQuotes === current.source.hasQuotes
    && previous.source.isPeerMessage === current.source.isPeerMessage
    && previous.source.isPeerHuman === current.source.isPeerHuman
    && previous.source.fromConv === current.source.fromConv
    && samePresentationValue(previous.source.backgroundCommand, current.source.backgroundCommand)
    && previous.source.steerFallback === current.source.steerFallback;
}
