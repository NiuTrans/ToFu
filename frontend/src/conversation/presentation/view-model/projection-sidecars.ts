/**
 * Typed sidecar-block projection for attachments, provenance, context, plans and
 * activity. Durable payloads remain immutable.
 * Entry: addProjectionSidecarBlocks.
 * Dependencies: conversation-sync.generated, errors, activity-anchor, types,
 * projection-values, translation-blocks, activity-blocks. */
import {
  type TurnActivityEntry,
  type TurnMessageInjection,
  type TurnRecord,
  type TurnRolledBackEntry,
  type TurnStallInjection,
} from '../../../api/conversation-sync.generated';
import {
  normalizeErrorEnvelope,
} from '../../../api/errors';
import {
  activityAnchorIndex,
  activityAnchorWindows,
  activityBlockLlmRound,
  activityEntryWithTiming,
  resolveActivityAnchorWindow,
} from '../activity-anchor';
import {
  type InjectionsBlockViewModel,
  type ConversationBlockViewModel,
  type TranslationDisplayMode,
  type TranslationActivity,
  type ConversationViewModelDiagnostics,
} from './types';
import {
  asArray,
  claimedBlockId,
} from './projection-values';
import {
  translatedPlanPreview,
} from './translation-blocks';
import {
  visibleActivityEntries,
  routeSnapshotActivityEntries,
} from './activity-blocks';

export function addProjectionSidecarBlocks(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
  translationMode: TranslationDisplayMode,
  translationActivity?: TranslationActivity,
): void {
  const projection = turn.projection;
  const contractBlockId = (proposed: string): string => claimedBlockId(
    turn.turnId, proposed, 'contract', claims, diagnostics,
  );
  if (projection.images?.length || projection.attachments?.length
      || projection.videos?.length
      || projection.pdfTexts?.length || projection.convRefs?.length
      || projection.replyQuotes?.length) {
    blocks.unshift({
      blockId: contractBlockId('attachments'),
      kind: 'attachments',
      identitySource: 'contract',
      source: projection,
      images: asArray(projection.images),
      videos: asArray(projection.videos),
      pdfTexts: asArray(projection.pdfTexts),
      mediaAttachments: asArray(projection.attachments),
      conversationReferences: asArray(projection.convRefs),
      replyQuotes: projection.replyQuotes ?? [],
    });
  }
  if (projection.provenance) {
    blocks.unshift({
      blockId: claimedBlockId(
        turn.turnId,
        projection.provenance.blockId,
        'contract',
        claims,
        diagnostics,
      ),
      kind: 'provenance',
      identitySource: 'contract',
      source: projection.provenance,
      value: projection.provenance,
    });
  }
  const leadingBlocks: ConversationBlockViewModel[] = [];
  if (projection.origin) {
    leadingBlocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.origin.blockId, 'contract', claims, diagnostics,
      ),
      kind: 'origin',
      identitySource: 'contract',
      source: projection.origin,
      value: projection.origin,
    });
  }
  if (projection.contextSnapshot) {
    leadingBlocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.contextSnapshot.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'context',
      identitySource: 'contract',
      source: projection.contextSnapshot,
      value: projection.contextSnapshot,
    });
  }
  if (leadingBlocks.length) blocks.unshift(...leadingBlocks);
  const injections: ReadonlyArray<[
    InjectionsBlockViewModel['channel'],
    ReadonlyArray<TurnMessageInjection | TurnStallInjection> | undefined,
  ]> = [
    ['inbox', projection._inboxInjects],
    ['peer', projection._peerInjects],
    ['user-steer', projection._userSteerInjects],
    ['background-command', projection._bgCommandInjects],
    ['stall-nudge', projection._stallNudges],
  ];
  /* The stall nudge is dual-recorded: sidecar chip AND a system_note segment
   * at its wire position. When the segment is present it owns the render —
   * skip the chip or the same intervention shows twice. The chip stays the
   * only render for turns that predate note recording. */
  const hasStallNoteSegment = (projection.segments ?? []).some((segment) => (
    segment.type === 'system_note' && segment.noteKind === 'intent-stall'
  ));
  for (const [channel, items] of injections) {
    if (!items?.length) continue;
    if (channel === 'stall-nudge' && hasStallNoteSegment) continue;
    for (const item of items) {
      if (channel === 'user-steer' && item.deliveryState === 'queued') {
        /* Missed the last model boundary; the queued human Turn is the one
         * visible delivery. Keep the prior receipt in authority for audit. */
        continue;
      }
      const round = Number.isInteger(item.round) && Number(item.round) >= 0
        ? Number(item.round) : null;
      const anchorLlmRound = round == null ? null : round - 1;
      const block: InjectionsBlockViewModel = {
        blockId: claimedBlockId(
          turn.turnId,
          item.blockId,
          'contract',
          claims,
          diagnostics,
        ),
        kind: 'injections',
        identitySource: 'contract',
        source: item,
        channel,
        items: [item],
        anchorLlmRound,
      };
      const anchorIndex = anchorLlmRound == null ? -1 : blocks.findIndex((candidate) => {
        if (candidate.kind === 'attachments' || candidate.kind === 'injections'
            || candidate.kind === 'provenance') return false;
        const candidateRound = activityBlockLlmRound(candidate);
        return candidateRound === anchorLlmRound;
      });
      if (anchorIndex >= 0) {
        blocks.splice(anchorIndex, 0, block);
      } else if (anchorLlmRound != null) {
        /* Anchor round missing: the inject was consumed on a round that
         * produced no tool calls / prose blocks (e.g. the final prose-only
         * round). Tail-dumping after EVERY block pushes the chip past the
         * assistant's answer text, reading as though the user steered after
         * the reply. Insert after the last block with a lower llmRound —
         * still above the terminal prose, truthful to "consumed at the end". */
        let insertAt = -1;
        for (let i = blocks.length - 1; i >= 0; i--) {
          const candidate = blocks[i];
          if (candidate.kind === 'attachments' || candidate.kind === 'injections'
              || candidate.kind === 'provenance') continue;
          const candidateRound = activityBlockLlmRound(candidate);
          if (candidateRound != null && candidateRound < anchorLlmRound) {
            insertAt = i + 1;
            break;
          }
        }
        blocks.splice(insertAt >= 0 ? insertAt : blocks.length, 0, block);
      } else {
        blocks.push(block);
      }
    }
  }
  const fileChanges = projection.fileChanges;
  if (fileChanges && fileChanges.files.length) {
    const files = fileChanges.files;
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId,
        fileChanges.blockId,
        'contract',
        claims,
        diagnostics,
      ),
      kind: 'file-changes',
      identitySource: 'contract',
      source: fileChanges,
      count: Math.max(fileChanges.count, projection.modifiedFiles ?? 0, files.length),
      files,
      state: fileChanges.state ?? 'applied',
      commandAvailable: Boolean(fileChanges.taskId) && fileChanges.undoAvailable !== false,
      ...(fileChanges.truncated ? { truncated: true } : {}),
      ...(fileChanges.error == null ? {} : { error: fileChanges.error }),
    });
  }
  if (projection.compaction) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.compaction.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'compaction',
      identitySource: 'contract',
      source: projection.compaction,
      value: projection.compaction,
      summaryMarkdown: projection.content ?? '',
    });
  }
  if (projection.imageGeneration) {
    blocks.unshift({
      blockId: claimedBlockId(
        turn.turnId, projection.imageGeneration.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'image-generation',
      identitySource: 'contract',
      source: projection.imageGeneration,
      value: projection.imageGeneration,
    });
  }
  if (projection.proposedPlan) {
    const translated = translatedPlanPreview(
      projection, projection.proposedPlan, translationActivity,
    );
    const displayMode = translated.markdown && translationMode === 'translated'
      ? 'translated' : 'original';
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.proposedPlan.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'proposed-plan',
      identitySource: 'contract',
      source: projection.proposedPlan,
      value: projection.proposedPlan,
      markdown: projection.proposedPlan.text,
      ...(translated.markdown
        ? { translatedMarkdown: translated.markdown } : {}),
      displayMarkdown: displayMode === 'translated'
        ? translated.markdown ?? projection.proposedPlan.text
        : projection.proposedPlan.text,
      displayMode,
      translationPending: translated.pending,
      translationStreaming: translated.streaming,
    });
  }
  if (projection.planExecution) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.planExecution.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'plan-execution',
      identitySource: 'contract',
      source: projection.planExecution,
      value: projection.planExecution,
    });
  }
  const activityTimeline = projection.activityTimeline;
  const activityEntries = [
    ...visibleActivityEntries(activityTimeline, projection.toolRounds ?? [], blocks)
      .map((entry) => activityEntryWithTiming(entry, projection.timingTrace)),
    ...routeSnapshotActivityEntries(projection.routeSnapshot),
  ];
  const terminalError = turn.status === 'failed'
    ? normalizeErrorEnvelope(turn.settlement?.error) : null;
  const terminalErrorEntry = terminalError
    ? [...activityEntries].reverse().find((entry) => (
      entry.kind === 'error' && entry.status === 'failed'
    ))
    : undefined;
  const terminalStatusCode = typeof terminalError?.statusCode === 'number'
    && Number.isInteger(terminalError.statusCode)
    ? terminalError.statusCode : undefined;
  if (activityEntries.length) {
    /* Diagnostic rows anchor inline where they happened: a tool-linked row
     * sits right under its tool block, everything else rides its 0-based
     * model round so the transcript keeps the interleaved
     * thinking → content → tool flow instead of one consolidated tail. */
    const anchorWindows = activityTimeline
      ? activityAnchorWindows(activityTimeline, blocks) : new Map();
    for (const entry of activityEntries) {
      const isTerminalErrorEntry = entry.id === terminalErrorEntry?.id
        && terminalError != null;
      const presentedEntry = isTerminalErrorEntry
        && entry.statusCode == null && terminalStatusCode != null
        ? { ...entry, statusCode: terminalStatusCode }
        : entry;
      const anchor = activityAnchorIndex(
        blocks, entry, resolveActivityAnchorWindow(anchorWindows, entry),
      );
      blocks.splice(anchor, 0, {
        blockId: claimedBlockId(
          turn.turnId, `activity:${entry.id}`, 'contract', claims, diagnostics,
        ),
        kind: 'activity-event',
        identitySource: 'contract',
        source: presentedEntry,
        value: presentedEntry,
        ...(isTerminalErrorEntry
          ? { terminalError } : {}),
      });
    }
  }
  if (terminalError && !terminalErrorEntry) {
    const terminalEntry: TurnActivityEntry = {
      id: 'terminal-error',
      spanId: 'terminal-error',
      seq: activityEntries[activityEntries.length - 1]?.seq ?? 0,
      occurredAt: turn.updatedAt,
      kind: 'error',
      status: 'failed',
      severity: terminalError.severity === 'warning' ? 'warning' : 'error',
      count: 1,
      summary: 'Turn failed',
      summaryKey: 'activity.error.failed',
      reasonCode: terminalError.kind,
      ...(terminalError.model ? { model: terminalError.model } : {}),
      ...(terminalStatusCode == null
        ? {} : { statusCode: terminalStatusCode }),
    };
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, 'activity:terminal-error', 'contract', claims, diagnostics,
      ),
      kind: 'activity-event',
      identitySource: 'contract',
      source: terminalEntry,
      value: terminalEntry,
      terminalError,
    });
  }

  const rolledBack = projection.rolledBack;
  if (rolledBack?.length) {
    const rolledBlocks: ConversationBlockViewModel[] = [];
    const rolledEntries: TurnRolledBackEntry[] = [];
    for (const entry of rolledBack) {
      if (!entry || typeof entry.blockId !== 'string'
          || !entry.blockId.trim()) continue;
      if (!String(entry.content ?? '').trim()
          && !String(entry.thinking ?? '').trim()) continue;
      rolledEntries.push(entry);
      rolledBlocks.push({
        blockId: claimedBlockId(
          turn.turnId, entry.blockId, 'contract', claims, diagnostics,
        ),
        kind: 'rolled-back',
        identitySource: 'contract',
        source: entry,
        value: entry,
      });
    }
    if (rolledBlocks.length) {
      /* The rewound tail belongs where it was generated: after the retained
       * rounds of the interrupted attempt, before the first block its
       * successor attempt streams. Anchor on durable attempt attribution —
       * present from the successor's first streamed token — instead of the
       * terminal lanes, which only exist after settlement: until then the
       * terminal anchor falls through to blocks.length and the rolled-back
       * block renders below everything the successor is still streaming
       * (the "previous thought stuck at the bottom" bug). Multi-resume turns
       * get one anchor per entry; turns predating attempt stamps fall back
       * to the terminal-lane anchor. */
      const attemptIdOf = (candidate: ConversationBlockViewModel): string => {
        const round = (candidate as { round?: unknown }).round;
        if (round && typeof round === 'object' && !Array.isArray(round)) {
          const id = (round as { attemptId?: unknown }).attemptId;
          if (typeof id === 'string' && id) return id;
        }
        const source = candidate.source as { attemptId?: unknown } | null;
        const id = source && typeof source === 'object'
          ? source.attemptId : undefined;
        return typeof id === 'string' ? id : '';
      };
      const successorBoundary = (rolledAttemptId: string): number => {
        if (!rolledAttemptId) return -1;
        let lastOwn = -1;
        blocks.forEach((candidate, index) => {
          if (attemptIdOf(candidate) === rolledAttemptId) lastOwn = index;
        });
        if (lastOwn < 0) return -1;
        for (let index = lastOwn + 1; index < blocks.length; index += 1) {
          const id = attemptIdOf(blocks[index]);
          if (id && id !== rolledAttemptId) return index;
        }
        return -1;
      };
      const currentAttemptId = typeof turn.currentAttemptId === 'string'
        ? turn.currentAttemptId : '';
      const firstTerminal = blocks.findIndex((candidate) => (
        (candidate.kind === 'text' || candidate.kind === 'thinking')
        && candidate.terminal
      ));
      const fallbackAnchor = currentAttemptId
        ? blocks.findIndex((candidate) => attemptIdOf(candidate) === currentAttemptId)
        : -1;
      const groupAnchor = fallbackAnchor >= 0
        ? fallbackAnchor
        : firstTerminal >= 0 ? firstTerminal : blocks.length;
      const inserts = new Map<number, ConversationBlockViewModel[]>();
      rolledBlocks.forEach((block, position) => {
        const entry = rolledEntries[position];
        const rolledAttemptId = entry && typeof entry.attemptId === 'string'
          ? entry.attemptId : '';
        const boundary = successorBoundary(rolledAttemptId);
        const anchor = boundary >= 0 ? boundary : groupAnchor;
        const bucket = inserts.get(anchor);
        if (bucket) bucket.push(block);
        else inserts.set(anchor, [block]);
      });
      const anchors = [...inserts.keys()].sort((a, b) => b - a);
      for (const anchor of anchors) {
        blocks.splice(anchor, 0, ...inserts.get(anchor)!);
      }
    }
  }
}
