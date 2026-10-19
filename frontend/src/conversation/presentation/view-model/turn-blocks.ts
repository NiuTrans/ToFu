/**
 * WeakMap-memoized Turn block selection. Owns projection pipeline order and local
 * translation cache keys; never mutates TurnStore.
 * Entry: selectTurnBlocks.
 * Dependencies: conversation-sync.generated, transient-turn, types, projection-values,
 * segment-blocks, program-blocks, projection-sidecars, block-coalescing. */
import {
  type TurnRecord,
} from '../../../api/conversation-sync.generated';
import {
  transientTurnPresentation,
} from '../../domain/transient-turn';
import {
  type ConversationBlockViewModel,
  type TranslationDisplayMode,
  type TranslationActivity,
  type ConversationViewModelDiagnostics,
} from './types';
import {
  claimedBlockId,
} from './projection-values';
import {
  richToolRoundsBySegment,
  blockFromSegment,
} from './segment-blocks';
import {
  addProgramBlocks,
} from './program-blocks';
import {
  addProjectionSidecarBlocks,
} from './projection-sidecars';
import { integrateContextProvenance } from '../context-provenance';

import { toolEffectNotice } from '../tool-effect-presentation';

import { selectTerminalPresentationSegments } from './terminal-segment-presentation';
import {
  coalesceAdjacentThinkingBlocks,
  coalesceAdjacentSystemNoteBlocks,
  settleThinkingActivity,
} from './block-coalescing';

/* Per-Turn projection cache.
 *
 * The reducer preserves a Turn object's identity whenever its projection is
 * unchanged, so a WeakMap keyed on the Turn object is a bounded, self-evicting
 * memo.  Without it every animation frame re-parses the entire segment array —
 * `selectTurnBlocks` is ~75% of the view-model cost on a long conversation —
 * even though a streaming delta only ever touches the tail Turn.  Caching also
 * keeps block object identities stable, which lets the keyed Surface skip its
 * structural comparison for untouched Turns.
 *
 * The only other inputs are the per-Turn display mode and translation activity,
 * both compared before reuse.  `diagnostics` is report-only (duplicate block-id
 * warnings), so a cache hit intentionally does not re-report. */
interface TurnBlocksMemoEntry {
  translationMode: TranslationDisplayMode;
  translationActivity: TranslationActivity | undefined;
  blocks: ReadonlyArray<ConversationBlockViewModel>;
}

const turnBlocksMemo = new WeakMap<TurnRecord, TurnBlocksMemoEntry>();

export function selectTurnBlocks(
  turn: TurnRecord,
  diagnostics: ConversationViewModelDiagnostics = {},
  translationMode: TranslationDisplayMode = turn.actor === 'human'
    ? 'original' : 'translated',
  translationActivity?: TranslationActivity,
): ReadonlyArray<ConversationBlockViewModel> {
  const cached = turnBlocksMemo.get(turn);
  if (cached
      && cached.translationMode === translationMode
      && cached.translationActivity === translationActivity) {
    return cached.blocks;
  }
  const segments = selectTerminalPresentationSegments(turn, turn.projection.segments ?? []);
  // Oversized terminal events can settle content while retaining an older
  // segment checkpoint. Present the durable answer after that checkpoint;
  // never infer settlement or mutate the authority to repair its display.
  const settled = ['completed', 'interrupted', 'truncated', 'failed'].includes(turn.status);
  const content = turn.projection.content;
  const hasTerminalText = segments.some(segment => segment.type === 'text'
    && (segment.terminal || segment.deliverable));
  const displaySegments = settled && !turn.projection.compaction
    && typeof content === 'string' && content.trim() && !hasTerminalText
    ? [...segments, {
      type: 'text' as const, blockId: 'text:terminal', text: content,
      terminal: true, deliverable: true,
    }]
    : segments;
  const claims = new Map<string, number>();

  const richRoundsBySegment = richToolRoundsBySegment(turn, displaySegments);
  /* The inbox lane renders through the durable ``_inboxInjects`` chip (the
   * ``<swarm-update>`` card). Turns persisted before the note was retired
   * also carry an ``inbox_delivery`` ``context-injection`` system_note with
   * the SAME payload — suppress it so the intervention does not render twice
   * (the chip is the single owner). */
  const hasInboxChip = Boolean(turn.projection._inboxInjects?.length);
  const hasPrograms = (turn.projection.toolRounds ?? []).some(round => round._programSynthetic === true);
  const blocks = displaySegments.flatMap((segment, index): ConversationBlockViewModel[] => {
    if (turn.projection.compaction && segment.type === 'text'
        && (segment.terminal || segment.deliverable)) return [];
    if (hasInboxChip && segment.type === 'system_note'
        && segment.noteKind === 'context-injection'
        && segment.reason === 'inbox_delivery') return [];
    // Preserve the no-parent fast path for long historical execute_tools logs.
    if (!hasPrograms && segment.type === 'tool_use' && segment.name === 'execute_tools') return [];
    const block = blockFromSegment(
      turn, segment, index, claims, diagnostics, translationMode,
      translationActivity, richRoundsBySegment,
    );

    /* Empty stream placeholders have no visual authority. Live progress is a
     * dedicated block, so retaining blank prose/thinking here only creates an
     * empty body between the header and the status surface. */
    if ((block.kind === 'text' || block.kind === 'thinking')
        && !block.displayMarkdown.trim()) return [];
    return [block];
  });
  addProgramBlocks(turn, blocks, claims, diagnostics);
  addProjectionSidecarBlocks(
    turn, blocks, claims, diagnostics, translationMode, translationActivity,
  );
  coalesceAdjacentThinkingBlocks(blocks);
  coalesceAdjacentSystemNoteBlocks(blocks);

  integrateContextProvenance(blocks);
  settleThinkingActivity(turn, blocks);
  const transient = transientTurnPresentation(turn);
  if (transient) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, 'live-status', 'contract', claims, diagnostics,
      ),
      kind: 'live-status',
      identitySource: 'contract',
      source: transient,
      value: transient,
    });
  }
  // Tool results can be unchanged while their containing attempt settles.
  // Include recovery copy in the semantic block key so Surface repaints it.
  blocks.forEach((block, index) => {
    if (block.kind === 'tool' || block.kind === 'program') {
      blocks[index] = { ...block, effectNotice: toolEffectNotice(block.round?.executionEffect, turn.status, block.round?.status) };
    }
  });
  turnBlocksMemo.set(turn, { translationMode, translationActivity, blocks });
  return blocks;
}
