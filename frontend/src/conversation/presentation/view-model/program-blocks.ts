/**
 * Linear program-parent placement over immutable durable tool/round evidence.
 * Entry: addProgramBlocks. Dependencies: Turn DTOs, round-outline, program-activity,
 * presentation types and the shared block identity claim owner. */
import type { TurnRecord, TurnToolRound } from '../../../api/conversation-sync.generated';
import { roundIdentityOf, type RoundIdentity } from '../round-outline';
import { createProgramGatewayResolver } from '../program-activity';
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

/** Index unique authored scopes once; missing legacy stamps are not conflicts. */
function compatibleRoundAnchor(identities: ReadonlyArray<RoundIdentity>): (identity: RoundIdentity) => string | undefined {
  const buckets = new Map<string, { count: number; key: string }>();
  const bucketKey = (mask: number, identity: RoundIdentity): string => JSON.stringify([
    mask, identity.llmRound, mask & 1 ? identity.attemptId : null, mask & 2 ? identity.taskId : null,
  ]);
  for (const identity of identities) {
    for (let mask = 0; mask < 4; mask += 1) {
      const key = bucketKey(mask, identity);
      const bucket = buckets.get(key);
      if (bucket) bucket.count += 1;
      else buckets.set(key, { count: 1, key: identity.key });
    }
  }
  return (identity) => {
    const mask = Number(Boolean(identity.attemptId)) | (Number(Boolean(identity.taskId)) << 1);
    let match: string | undefined;
    for (const attemptId of identity.attemptId ? [identity.attemptId, ''] : ['']) {
      for (const taskId of identity.taskId ? [identity.taskId, ''] : ['']) {
        const bucket = buckets.get(bucketKey(mask, { ...identity, attemptId, taskId }));
        if (!bucket) continue;
        if (bucket.count !== 1 || match !== undefined) return undefined;
        match = bucket.key;
      }
    }
    return match;
  };
}

/** Shared identity owner for gateway replacement and legacy sidecar placement. */
function programBlockFromRound(
  turn: TurnRecord,
  round: TurnToolRound,
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
  roundIndex: number,
): ProgramBlockViewModel {
  const rawCallId = round._programCallId;
  const programCallId = typeof rawCallId === 'string' && rawCallId.trim()
    ? rawCallId : `round-${String(round.roundNum ?? roundIndex)}`;
  return {
    blockId: claimedBlockId(
      turn.turnId, `program:${programCallId}`, 'contract', claims, diagnostics,
    ),
    kind: 'program', identitySource: 'contract', source: round, programCallId, round,
  };
}

export function addProgramBlocks(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
): void {
  const rounds = turn.projection.toolRounds ?? [];
  // Claim every parent in durable sidecar order, independently of which gateway
  // segments have arrived. Reused IDs must not swap disclosure/focus owners.
  const programBlocks = new Map<TurnToolRound, ProgramBlockViewModel>();
  rounds.forEach((round, index) => {
    if (round._programSynthetic === true) {
      programBlocks.set(round, programBlockFromRound(turn, round, claims, diagnostics, index));
    }
  });
  if (!programBlocks.size) return;

  let resolveGateway: ((value: unknown) => TurnToolRound | undefined) | undefined;
  const slots = new Map<TurnToolRound, { index: number; score: number; count: number }>();
  blocks.forEach((block, index) => {
    if (block.kind !== 'tool' || (block.name !== 'execute_program' && block.name !== 'execute_tools')) return;
    // Rich rows can be greedily paired across reused provider IDs. The ordered
    // segment's explicit round/scope remains authoritative for replacement.
    const identity = roundIdentityOf(block.source) ?? roundIdentityOf(block.round);
    const gateway: Readonly<Record<string, unknown>> = {
      ...identity, toolName: block.name, toolCallId: block.toolCallId,
    };
    resolveGateway ??= createProgramGatewayResolver([...programBlocks.keys()]);
    const owner = resolveGateway(gateway);
    if (!owner) return;
    // A scoped occurrence wins over a compatible unstamped historical shell.
    // Equally supported occurrences are ambiguous: retain their raw evidence.
    const score = Number(Boolean((gateway.attemptId ?? gateway._attemptId)
      && (owner.attemptId ?? owner._attemptId)))
      + Number(Boolean((gateway.taskId ?? gateway._taskId) && (owner.taskId ?? owner._taskId)));
    const previous = slots.get(owner);
    if (!previous || score > previous.score) slots.set(owner, { index, score, count: 1 });
    else if (score === previous.score) previous.count += 1;
  });
  const replacements = new Map<number, ProgramBlockViewModel>();
  for (const [owner, slot] of slots) {
    if (slot.count === 1) replacements.set(slot.index, programBlocks.get(owner)!);
  }
  let writeIndex = 0;
  for (let readIndex = 0; readIndex < blocks.length; readIndex++) {
    const block = blocks[readIndex];
    const replacement = replacements.get(readIndex);
    if (replacement) blocks[writeIndex++] = replacement;
    else if (block.kind !== 'tool' || block.name !== 'execute_tools') blocks[writeIndex++] = block;
  }
  blocks.length = writeIndex;
  const programIndices: number[] = [];
  rounds.forEach((round, index) => {
    if (programBlocks.has(round) && slots.get(round)?.count !== 1) programIndices.push(index);
  });
  if (!programIndices.length) return;

  // Only original tool nodes can be child anchors. Their order does not change
  // when parents are inserted, including repeated ids from resumed attempts.
  const firstToolByCall = new Map<string, PlacementNode>();

  const anchoredPrograms = new Map<TurnToolRound, PlacementNode>();
  const lastNodeByRound = new Map<string | null, PlacementNode>();
  const authoredIdentities = new Map<string, RoundIdentity>();
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

    if (block.kind === 'program') anchoredPrograms.set(block.round, node);
    if (block.kind === 'tool') {
      const callId = block.toolCallId;
      if (!firstToolByCall.has(callId)) firstToolByCall.set(callId, node);
    }
    const identity = roundIdentityOf(block.source);
    lastNodeByRound.set(identity?.key ?? null, node);
    if (identity) authoredIdentities.set(identity.key, identity);
  }
  const resolveRoundAnchor = compatibleRoundAnchor([...authoredIdentities.values()]);

  // Legacy rows with no gateway or authored round anchor may still use the
  // earliest visible later tool. A suffix minimum keeps that fallback linear;
  // it must never override known same-occurrence chronology.
  const followingToolPosition = new Array<number>(rounds.length + 1);
  const followingProgramPosition = new Array<number>(rounds.length + 1);
  followingToolPosition[rounds.length] = Infinity;
  followingProgramPosition[rounds.length] = Infinity;
  for (let index = rounds.length - 1; index >= 0; index -= 1) {
    const callId = rounds[index].toolCallId;
    const position = typeof callId === 'string' && callId
      ? firstToolByCall.get(callId)?.position ?? Infinity : Infinity;
    const programPosition = anchoredPrograms.get(rounds[index])?.position ?? Infinity;
    followingProgramPosition[index] = Math.min(programPosition, followingProgramPosition[index + 1]);
    followingToolPosition[index] = Math.min(position, programPosition, followingToolPosition[index + 1]);
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
    const block = programBlocks.get(round)!;
    let anchorPosition = Infinity;
    for (const childId of Array.isArray(round.childCallIds) ? round.childCallIds : []) {
      if (typeof childId !== 'string' || !childId) continue;
      anchorPosition = Math.min(anchorPosition, firstToolByCall.get(childId)?.position ?? Infinity);
    }
    const identity = roundIdentityOf(round);
    let roundKey = identity?.key ?? null;
    if (identity && !lastNodeByRound.has(roundKey)) {
      roundKey = resolveRoundAnchor(identity) ?? roundKey;
    }
    const previousRoundNode = lastNodeByRound.get(roundKey);
    if (anchorPosition === Infinity) {
      // A later parent's gateway may hydrate first. Its slot bounds the missing
      // earlier parent even when it is also the last authored same-round node.
      const nextProgramPosition = followingProgramPosition[roundIndex + 1];
      if (previousRoundNode && previousRoundNode.position >= nextProgramPosition) {
        anchorPosition = nextProgramPosition;
      } else if (!previousRoundNode || roundKey === null) {
        anchorPosition = followingToolPosition[roundIndex + 1];
      }
    }
    const node: PlacementNode = { block, position: 0, previous: null, next: null };
    if (anchorPosition !== Infinity) {
      const anchor = originalNodes[(anchorPosition - 1) / 2];
      node.position = anchorPosition - 1;
      insertBetween(node, anchor.previous, anchor);
      // A tool anchor appends to its gap, so equality also means the new parent
      // follows the old round marker. A marker beyond this gap remains latest.
      if (!previousRoundNode || previousRoundNode.position <= node.position) {
        lastNodeByRound.set(roundKey, node);
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
      lastNodeByRound.set(roundKey, node);
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
