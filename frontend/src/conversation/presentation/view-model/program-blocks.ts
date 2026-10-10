/**
 * Linear program-parent placement over immutable durable tool/round evidence.
 * Entry: addProgramBlocks. Dependencies: generated Turn DTOs, activity-anchor,
 * presentation types and the shared block identity claim owner. */
import type { TurnRecord } from '../../../api/conversation-sync.generated';
import { activityBlockLlmRound } from '../activity-anchor';
import type {
  ProgramBlockViewModel,
  ConversationBlockViewModel,
  ConversationViewModelDiagnostics,
} from './types';
import { claimedBlockId } from './projection-values';

interface PlacementNode {
  block: ConversationBlockViewModel;
  /** Original blocks occupy odd slots; inserted parents occupy the even gaps. */
  position: number;
  previous: PlacementNode | null;
  next: PlacementNode | null;
}

export function addProgramBlocks(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
): void {
  const rounds = turn.projection.toolRounds ?? [];
  const programIndices: number[] = [];
  rounds.forEach((round, index) => {
    if (round._programSynthetic === true) programIndices.push(index);
  });
  if (!programIndices.length) return;

  // Only original tool nodes can be child anchors. Their order does not change
  // when parents are inserted, including repeated ids from resumed attempts.
  const firstToolByCall = new Map<string, PlacementNode>();
  const lastNodeByRound = new Map<number | null, PlacementNode>();
  const originalNodes: PlacementNode[] = [];
  let head: PlacementNode | null = null;
  let tail: PlacementNode | null = null;
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index];
    const node: PlacementNode = { block, position: index * 2 + 1, previous: tail, next: null };
    if (tail) tail.next = node;
    else head = node;
    tail = node;
    originalNodes.push(node);
    if (block.kind === 'tool') {
      const callId = block.toolCallId;
      if (!firstToolByCall.has(callId)) firstToolByCall.set(callId, node);
    }
    lastNodeByRound.set(activityBlockLlmRound(block), node);
  }

  // The old suffix sets selected the earliest visible tool matching ANY later
  // round, rather than the next round's tool. Preserve that exact rule with a
  // suffix minimum, without slicing and re-scanning the rest of the transcript.
  const followingToolPosition = new Array<number>(rounds.length + 1);
  followingToolPosition[rounds.length] = Infinity;
  for (let index = rounds.length - 1; index >= 0; index -= 1) {
    const callId = rounds[index].toolCallId;
    const position = typeof callId === 'string' && callId
      ? firstToolByCall.get(callId)?.position ?? Infinity : Infinity;
    followingToolPosition[index] = Math.min(position, followingToolPosition[index + 1]);
  }

  const insertBetween = (
    node: PlacementNode,
    previous: PlacementNode | null,
    next: PlacementNode | null,
  ): void => {
    node.previous = previous;
    node.next = next;
    if (previous) previous.next = node;
    else head = node;
    if (next) next.previous = node;
    else tail = node;
  };

  for (const roundIndex of programIndices) {
    const round = rounds[roundIndex];
    const rawCallId = round._programCallId;
    const programCallId = typeof rawCallId === 'string' && rawCallId.trim()
      ? rawCallId : `round-${String(round.roundNum ?? roundIndex)}`;
    const block: ProgramBlockViewModel = {
      blockId: claimedBlockId(
        turn.turnId, `program:${programCallId}`, 'contract', claims, diagnostics,
      ),
      kind: 'program', identitySource: 'contract', source: round, programCallId, round,
    };
    let anchorPosition = Infinity;
    for (const childId of Array.isArray(round.childCallIds) ? round.childCallIds : []) {
      if (typeof childId !== 'string' || !childId) continue;
      anchorPosition = Math.min(anchorPosition, firstToolByCall.get(childId)?.position ?? Infinity);
    }
    if (anchorPosition === Infinity) anchorPosition = followingToolPosition[roundIndex + 1];
    const llmRound = activityBlockLlmRound(block);
    const previousRoundNode = lastNodeByRound.get(llmRound);
    const node: PlacementNode = { block, position: 0, previous: null, next: null };
    if (anchorPosition !== Infinity) {
      const anchor = originalNodes[(anchorPosition - 1) / 2];
      node.position = anchorPosition - 1;
      insertBetween(node, anchor.previous, anchor);
      // A tool anchor appends to its gap, so equality also means the new parent
      // follows the old round marker. A marker beyond this gap remains latest.
      if (!previousRoundNode || previousRoundNode.position <= node.position) {
        lastNodeByRound.set(llmRound, node);
      }
    } else {
      // Childless failures follow the last block of their authored round. That
      // marker may itself be an earlier inserted parent, including one sitting
      // between parents of other rounds. Linked insertion preserves that order.
      const previous = previousRoundNode ?? tail;
      node.position = previous
        ? previous.position + (previous.position % 2 === 1 ? 1 : 0)
        : originalNodes.length * 2;
      insertBetween(node, previous, previous?.next ?? null);
      lastNodeByRound.set(llmRound, node);
    }
  }
  // The constructor owns this array. Rebuild it once; no durable record, source
  // array or block is mutated, and no index or linked node survives this call.
  blocks.length = 0;
  let current: PlacementNode | null = head;
  while (current) {
    blocks.push(current.block);
    current = current.next;
  }
}
