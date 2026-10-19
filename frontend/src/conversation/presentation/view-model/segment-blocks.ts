import { createToolOccurrenceMatcher } from '../tool-occurrences';
/**
 * Ordered durable segment projection and occurrence-matched rich tool rounds. Receives
 * stable identity and translation helpers.
 * Entry: richToolRoundsBySegment, blockFromSegment.
 * Dependencies: conversation-sync.generated, types, projection-values, translation-
 * blocks. */
import {
  type TurnContentSegment,
  type TurnRecord,
  type TurnToolRound,
} from '../../../api/conversation-sync.generated';
import {
  type BlockIdentitySource,
  type ConversationBlockViewModel,
  type TranslationDisplayMode,
  type TranslationActivity,
  type ConversationViewModelDiagnostics,
} from './types';
import {
  claimedBlockId,
} from './projection-values';
import {
  withoutAuthoritativeProposedPlan,
  translatedProposedPlanBody,
} from './translation-blocks';

import { contextNoteDisplayKind } from '../context-note-grouping';

export function richToolRoundsBySegment(
  turn: TurnRecord,
  segments: ReadonlyArray<TurnContentSegment> = turn.projection.segments ?? [],
): ReadonlyArray<TurnToolRound | undefined> {
  const rounds = turn.projection.toolRounds ?? [];
  const resolve = createToolOccurrenceMatcher(rounds.filter(round => round.toolCallId));
  return segments.map((segment) => {
    if (segment.type !== 'tool_use') return undefined;
    // Embedded bodies still consume their occurrence, keeping later pairings stable.
    const round = resolve(segment.id.trim(), segment);
    return segment._round ?? round;
  });
}

export function blockFromSegment(
  turn: TurnRecord,
  segment: TurnContentSegment,
  occurrence: number,
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
  translationMode: TranslationDisplayMode,
  translationActivity: TranslationActivity | undefined,
  richRoundsBySegment: ReadonlyArray<TurnToolRound | undefined>,
): ConversationBlockViewModel {
  const identitySource: BlockIdentitySource = 'contract';
  const blockId = claimedBlockId(
    turn.turnId, segment.blockId,
    identitySource,
    claims,
    diagnostics,
  );
  if (segment.type === 'text') {
    const authoritative = (segment.terminal || segment.deliverable)
      ? turn.projection.content ?? segment.text : segment.text;
    const humanOriginal = turn.actor === 'human'
      && (segment.terminal || segment.deliverable)
      ? turn.projection.originalContent : undefined;
    const rawOriginal = humanOriginal || authoritative;
    const translated = humanOriginal ? authoritative : segment.translatedText
      || ((segment.terminal || segment.deliverable)
        ? turn.projection.translatedContent : undefined);
    const ownsProposedPlan = Boolean(
      turn.projection.proposedPlan && (segment.terminal || segment.deliverable),
    );
    const original = ownsProposedPlan
      ? withoutAuthoritativeProposedPlan(
        rawOriginal, turn.projection.proposedPlan?.text,
      ) : rawOriginal;
    const visibleTranslation = translated && ownsProposedPlan
      ? (translatedProposedPlanBody(translated, false)
        ? withoutAuthoritativeProposedPlan(translated) : undefined)
      : translated;
    const preview = !translated && translationActivity?.status === 'pending'
      ? translationActivity.partialByRound?.[segment.blockId] : undefined;
    const visiblePreview = preview && preview.trim()
      ? (ownsProposedPlan ? withoutAuthoritativeProposedPlan(preview) : preview) : undefined;
    const displayedTranslation = visibleTranslation ?? visiblePreview;
    const displayMode = displayedTranslation && translationMode === 'translated'
      ? 'translated' : 'original';
    return {
      blockId,
      kind: 'text',
      identitySource,
      source: segment,
      markdown: original,
      ...((segment.terminal || segment.deliverable)
        && turn.projection.translation?.model?.trim()
        ? { translationModel: turn.projection.translation.model } : {}),
      ...((segment.terminal || segment.deliverable) && turn.actor === 'human'
        && !humanOriginal && !visibleTranslation?.trim()
        && turn.projection.translation?.status === 'completed'
        ? { inputTranslationCompleted: true } : {}),
      ...(visibleTranslation ? { translatedMarkdown: visibleTranslation } : {}),
      displayMarkdown: displayMode === 'translated'
        ? displayedTranslation ?? original : original,
      displayMode,
      deliverable: Boolean(segment.deliverable),
      terminal: Boolean(segment.terminal),
      resumable: Boolean(segment.resumable),
    };
  }
  if (segment.type === 'thinking') {
    const markdown = segment.terminal
      ? turn.projection.thinking ?? segment.text : segment.text;
    /* Closed reasoning is immutable history, so its translation is pinned
     * onto the segment (translatedText). While the translation worker is
     * still catching up, the live per-round preview (partialByRound, keyed
     * by the segment's blockId) already renders in its place — the reader
     * never has to reopen the turn to see reasoning in the UI language.
     * The preview is presentation-only: it never becomes translatedMarkdown,
     * so toggle/availability semantics keep tracking durable facts. */
    const durable = segment.translatedText;
    const preview = !durable && translationActivity?.status === 'pending'
      ? translationActivity.partialByRound?.[segment.blockId] : undefined;
    const livePreview = preview && preview.trim() ? preview : undefined;
    const displayMode = (durable || livePreview)
      && translationMode === 'translated' ? 'translated' : 'original';
    return {
      blockId,
      kind: 'thinking',
      identitySource,
      source: segment,
      markdown,
      ...(durable ? { translatedMarkdown: durable } : {}),
      displayMarkdown: displayMode === 'translated'
        ? durable ?? livePreview ?? markdown : markdown,
      displayMode,
      terminal: Boolean(segment.terminal),
      memberBlockIds: [blockId],
      ...(segment.signature ? { signature: segment.signature } : {}),
    };
  }
  if (segment.type === 'system_note') {
    return {
      blockId,
      kind: 'system-note',
      identitySource,
      source: segment,
      noteKind: contextNoteDisplayKind(segment) as typeof segment.noteKind,
      ...(typeof segment.injectionId === 'string' && segment.injectionId
        ? { injectionId: segment.injectionId } : {}),
      action: segment.action ?? 'prompt',
      reason: segment.reason,
      text: segment.text,
      llmRound: typeof segment.llmRound === 'number' ? segment.llmRound : null,
    };
  }
  const richRound = richRoundsBySegment[occurrence] ?? segment._round;
  return {
    blockId,
    kind: 'tool',
    identitySource,
    source: segment,
    toolCallId: segment.id,
    name: segment.name,
    input: segment.input,
    result: segment.result,
    ...(richRound ? { round: richRound } : {}),
  };
}
