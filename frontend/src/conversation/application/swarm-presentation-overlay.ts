import { applyMessageReceipt, applyMessageReceipts, coalesceMessageReceipts, projectMessageReceipts, messageReceiptIdentity, sameMessageReceipt } from '../presentation/agent-message-receipts';
import { entityRecordValues } from '../domain/turn-store';
/**
 * Compose conversation-scoped swarm telemetry over authoritative Turn state.
 *
 * Swarm push frames are lifecycle presentation, not durable transcript facts.
 * The overlay therefore owns only swarm-specific round fields and is rebased
 * onto the newest TurnStore revision before every update and Surface render.
 */
import type { PushTransportEnvelope } from '../../api/event-contract.generated';
import type {
  TurnContentSegment,
  TurnProjection,
  TurnRecord,
  TurnToolRound,
} from '../../api/conversation-sync.generated';
import type { TurnState } from '../domain/turn-store';
import { swarmLaunchSelection, swarmRoundAgentIds, swarmRoundRunId, swarmRoundExplicitRunId, swarmFrameMatchesExecution, swarmFrameExecutionTaskId } from '../presentation/swarm-agent-presentation';

export type SwarmOverlayReader = (
  conversationId: string,
  turnId: string,
) => TurnRecord | null;

type MutableProjection = Omit<TurnProjection, 'segments' | 'toolRounds'> & {
  segments?: TurnContentSegment[];
  toolRounds?: TurnToolRound[];
  _swarmRoundNum?: unknown;
};

const SWARM_ROUND_PRESENTATION_FIELDS = Object.freeze([
  'query',
  'status',
  '_swarm',
  '_swarmActive',
  '_asyncRunning',
  '_swarmStartTime',
  '_swarmEndTime',
  '_elapsed',
  '_swarmStats',
  '_swarmError',
  '_swarmKey',
  '_swarmRunId',
  '_swarmAgents',

  '_swarmMessageReceipts',
  '_swActiveConfirmedAt',
] as const);

function cloneValue<T>(value: T): T {
  if (value == null) return value;
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(value);
    } catch {
      // Conversation projections are JSON-shaped; retain the lean fallback.
    }
  }
  return JSON.parse(JSON.stringify(value)) as T;
}

function mutableProjection(projection: TurnProjection): MutableProjection {
  const copied = cloneValue(projection ?? {}) as MutableProjection;
  copied.toolRounds = Array.isArray(copied.toolRounds)
    ? copied.toolRounds
    : [];
  projectMessageReceipts(copied);
  return copied;
}

function copyTurn(
  turn: TurnRecord,
  advanceRevision: boolean,
  updatedAt: number = Date.now(),
): TurnRecord {
  return {
    ...turn,
    projection: mutableProjection(turn.projection),
    projectionRevision: Number(turn.projectionRevision || 0)
      + (advanceRevision ? 1 : 0),
    updatedAt: advanceRevision ? updatedAt : turn.updatedAt,
  };
}

function hasSwarm(turn: TurnRecord | null | undefined): boolean {
  return (turn?.projection?.toolRounds ?? []).some((round) =>
    Boolean(round && (round._swarm || round.toolName === 'spawn_agents' || round._agentMessageReceipt)));
}

function swarmSettled(round: TurnToolRound): boolean {
  return Boolean((round._swarmSnapshot as { settled?: boolean } | undefined)?.settled);
}

const swarmRunId = swarmRoundRunId;

function sameSwarmScope(left: TurnToolRound, right: TurnToolRound): boolean {
  if (left.taskId && right.taskId && left.taskId !== right.taskId) return false;
  if (left.attemptId && right.attemptId && left.attemptId !== right.attemptId) return false;
  const leftRun = swarmRoundExplicitRunId(left), rightRun = swarmRoundExplicitRunId(right);
  if (leftRun && rightRun && leftRun !== rightRun) return false;
  const leftAgents = swarmRoundAgentIds(left), rightAgents = swarmRoundAgentIds(right);
  return !leftAgents.length || !rightAgents.length
    || leftAgents.some(id => rightAgents.includes(id));
}

function sameSwarmRound(left: TurnToolRound, right: TurnToolRound): boolean {
  if (!(left._swarm || left.toolName === 'spawn_agents')
      || !(right._swarm || right.toolName === 'spawn_agents')
      || !sameSwarmScope(left, right)) return false;
  if (left.roundNum != null && right.roundNum != null
      && Number(left.roundNum) !== Number(right.roundNum)) return false;
  if (left.llmRound != null && right.llmRound != null
      && Number(left.llmRound) !== Number(right.llmRound)) return false;
  if (left.toolCallId && right.toolCallId) return left.toolCallId === right.toolCallId;
  return left.roundNum != null && right.roundNum != null
    || left.llmRound != null && right.llmRound != null;
}

function sameExecutionScope(
  segment: TurnContentSegment,
  round: TurnToolRound,
): boolean {
  const segmentAttemptId = String(segment.attemptId ?? '');
  const roundAttemptId = String(round.attemptId ?? '');
  if (segmentAttemptId || roundAttemptId) {
    return Boolean(segmentAttemptId)
      && segmentAttemptId === roundAttemptId;
  }
  const segmentTaskId = String(segment.taskId ?? '');
  const roundTaskId = String(round.taskId ?? '');
  if (segmentTaskId || roundTaskId) {
    return Boolean(segmentTaskId) && segmentTaskId === roundTaskId;
  }
  return true;
}

function uniqueLegacyRound(
  segment: TurnContentSegment,
  rounds: TurnToolRound[],
): TurnToolRound | undefined {
  if (segment.llmRound == null) return undefined;
  const candidates = rounds.filter((candidate) =>
    candidate.llmRound != null
    && Number(candidate.llmRound) === Number(segment.llmRound)
    && sameExecutionScope(segment, candidate));
  return candidates.length === 1 ? candidates[0] : undefined;
}

function rebindSegments(projection: TurnProjection): void {
  const mutable = projection as MutableProjection;
  if (!Array.isArray(mutable.segments)
      || !Array.isArray(mutable.toolRounds)) return;
  const roundsByCallId = new Map<string, TurnToolRound[]>();
  const consumed = new Set<TurnToolRound>();
  for (const round of mutable.toolRounds) {
    const callId = String(round?.toolCallId ?? '');
    if (!callId) continue;
    const matches = roundsByCallId.get(callId) ?? [];
    matches.push(round);
    roundsByCallId.set(callId, matches);
  }
  mutable.segments = mutable.segments.map((segment) => {
    if (!segment || segment.type !== 'tool_use') return segment;
    const callId = String(segment.id ?? '');
    // Provider call IDs can repeat, even within an attempt. Consume ordered
    // occurrences only in this segment's execution scope; never borrow from
    // another attempt or fall back to llmRound for an unmatched call ID.
    const round = callId
      ? roundsByCallId.get(callId)?.find((candidate) =>
        !consumed.has(candidate) && sameExecutionScope(segment, candidate))
      : uniqueLegacyRound(segment, mutable.toolRounds ?? []);
    if (round) {
      consumed.add(round);
      return { ...segment, _round: round };
    }
    return segment;
  });
}

function rebase(
  durable: TurnRecord | null | undefined,
  previousOverlay: TurnRecord | null | undefined,
): TurnRecord | null {
  if (!durable) {
    return previousOverlay ? copyTurn(previousOverlay, false) : null;
  }
  if (!previousOverlay || !hasSwarm(previousOverlay)) {
    return copyTurn(durable, false);
  }

  const projection = mutableProjection(durable.projection);
  const rebased: TurnRecord = {
    ...durable,
    projection,
    projectionRevision: Math.max(
      Number(durable.projectionRevision || 0),
      Number(previousOverlay.projectionRevision || 0),
    ),
    updatedAt: Math.max(
      Number(durable.updatedAt || 0),
      Number(previousOverlay.updatedAt || 0),
    ),
  };
  const previousProjection = previousOverlay.projection as MutableProjection;
  if (Object.prototype.hasOwnProperty.call(previousProjection, '_swarmRoundNum')) {
    projection._swarmRoundNum = cloneValue(previousProjection._swarmRoundNum);
  }

  const consumed = new Set<TurnToolRound>();
  for (const [previousIndex, previousRound] of (
    previousProjection.toolRounds ?? []
  ).entries()) {
    if (!previousRound
        || !(previousRound._swarm || previousRound.toolName === 'spawn_agents')) {
      continue;
    }
    const matches = (projection.toolRounds ?? []).filter(candidate =>
      !consumed.has(candidate) && sameSwarmRound(candidate, previousRound));
    if (matches.length > 1) continue; // Ambiguity is not occurrence evidence.
    let targetIndex = matches.length === 1 ? projection.toolRounds!.indexOf(matches[0]) : -1;
    if (targetIndex < 0 && !previousRound.toolCallId) {
      // A provisional push panel has no call ID. Bind only to a unique
      // durable launch in its execution; wait/result tools are not launches.
      const launches = (projection.toolRounds ?? []).filter((candidate) =>
        candidate.toolName === 'spawn_agents' && !consumed.has(candidate)
        && sameSwarmScope(candidate, previousRound));
      if (launches.length > 1) continue;
      if (launches.length === 1) targetIndex = projection.toolRounds!.indexOf(launches[0]);
    }
    if (targetIndex < 0) {
      // A push can narrowly precede the authoritative spawning-tool revision.

      // A reused provider ID with incompatible launch evidence is replaced
      // authority, not a newly arriving provisional row. Never duplicate it.
      if (previousRound.toolCallId && projection.toolRounds?.some(candidate =>
        candidate.toolCallId === previousRound.toolCallId)) continue;
      targetIndex = Math.min(
        Math.max(0, previousIndex),
        projection.toolRounds?.length ?? 0,
      );
      projection.toolRounds?.splice(
        targetIndex,
        0,
        cloneValue(previousRound),
      );
      continue;
    }
    const target = projection.toolRounds?.[targetIndex];
    if (!target || swarmSettled(target)) continue;

    consumed.add(target);
    const merged: TurnToolRound = { ...target };
    const previousFields = previousRound as Record<string, unknown>;
    const mergedFields = merged as Record<string, unknown>;
    for (const field of SWARM_ROUND_PRESENTATION_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(previousFields, field)) {
        mergedFields[field] = cloneValue(previousFields[field]);
      }
    }
    if (projection.toolRounds) projection.toolRounds[targetIndex] = merged;

    consumed.add(merged);
  }
  for (const round of previousProjection.toolRounds ?? []) {
    if (round._agentMessageReceipt) applyMessageReceipt(projection, round._agentMessageReceipt as Record<string, unknown>);
  }
  projectMessageReceipts(projection);
  rebindSegments(projection);
  return rebased;
}

function newestAssistantTurns(state: TurnState): TurnRecord[] {
  const orderedIds = Object.values(state?.laneOrder ?? {})
    .flatMap((lane) => lane ?? []);
  return [...new Set(orderedIds)]
    .reverse()
    .map((turnId) => state.turnsById[turnId])
    .filter((turn): turn is TurnRecord => Boolean(
      turn && (turn.actor === 'assistant' || turn.actor === 'planner'),
    ));
}

function selectCandidates(
  conversationId: string,
  state: TurnState,
  readOverlay: SwarmOverlayReader,
): TurnRecord[] {
  const turns = newestAssistantTurns(state).map((durable) => rebase(
    durable,
    readOverlay(conversationId, durable.turnId),
  ) ?? durable);
  projectMessageReceipts({ toolRounds: turns.flatMap(turn => turn.projection.toolRounds ?? []) });
  return turns;
}

function compose(
  conversationId: string,
  durableState: TurnState,
  transientState: TurnState,
  readOverlay: SwarmOverlayReader,
  candidateLimit = 64,
): TurnState {
  let turnsById = transientState.turnsById;
  let changed = false;
  for (const durable of newestAssistantTurns(durableState).slice(0, candidateLimit)) {
    const overlay = readOverlay(conversationId, durable.turnId);
    if (!overlay && !hasSwarm(durable)) continue;
    if (!changed) turnsById = { ...turnsById };
    turnsById[durable.turnId] = rebase(durable, overlay) ?? durable;
    changed = true;
  }
  if (changed) {
    const rounds = Object.values(turnsById).flatMap(turn => turn?.projection.toolRounds ?? []);
    const receipts = rounds.flatMap(round => {
      const snapshot = round._swarmSnapshot as { agents?: Record<string, any>[]; messageReceipts?: Record<string, any>[] } | undefined;
      return [...(Array.isArray(round._swarmMessageReceipts) ? round._swarmMessageReceipts : []),
        ...(snapshot?.messageReceipts ?? []),
        ...[...(Array.isArray(round._swarmAgents) ? round._swarmAgents : []), ...(snapshot?.agents ?? [])]
          .flatMap((agent: any) => agent.messageReceipts ?? [])];
    });
    const senders = new Map<string, TurnToolRound>();
    const receiptKey = (receipt: Record<string, any>) => JSON.stringify([
      receipt.swarmRunId, receipt.agentId, receipt.messageId,
    ]);
    for (const round of rounds) {
      if (round.toolName !== 'send_agent_message') continue;
      const identity = messageReceiptIdentity(round);
      const key = receiptKey(identity);
      if (sameMessageReceipt(identity, identity) && !senders.has(key)) senders.set(key, round);
    }
    const enrichedReceipts = coalesceMessageReceipts(receipts).map(receipt => {
      const sender = senders.get(receiptKey(receipt));
      let content: unknown;
      try { content = typeof sender?.toolArgs === 'string' ? JSON.parse(sender.toolArgs).content
        : (sender?.toolArgs as Record<string, unknown> | undefined)?.content; } catch { /* Body unavailable. */ }
      return { ...receipt, ...(typeof receipt.content !== 'string' && typeof content === 'string' ? { content } : {}) };
    });
    if (enrichedReceipts.length) for (const turn of Object.values(turnsById)) {
      if (!turn) continue;
      // One private copy per Turn, regardless of receipt count. The batch
      // mutates only this copy and never the authoritative/transient inputs.
      const copy = copyTurn(turn, false);
      if (applyMessageReceipts(copy.projection, enrichedReceipts)) turnsById[turn.turnId] = copy;
    }
  }
  return changed ? { ...transientState, turnsById } : transientState;
}

export const swarmPresentationOverlay = Object.freeze({
  advance(turn: TurnRecord, updatedAt: number = Date.now()): TurnRecord {
    return copyTurn(turn, true, updatedAt);
  },
  compose,
  hasSwarm,
  rebase,
  rebindSegments,
  selectCandidates,
});

import { createSwarmFailureNotices, type SwarmFailureNotice, type SwarmTranslate } from '../presentation/swarm-failure-presentation';

export interface SwarmConversationReference {
  id: string;
  [key: string]: unknown;
}

export interface SwarmPushFrame extends Partial<PushTransportEnvelope> {
  type: string;
  /** Read compatibility for pre-canonical conversation mirrors. */
  convId?: string;
  phase?: string;
  [key: string]: unknown;
}

export interface SwarmPresentationContext {
  convId: string;
  /** Explicit producing execution task only; empty when the frame does not carry one. */
  taskId: string;
  assistantProjection: TurnProjection;
}

export interface SwarmPushPresentationPorts {

  translate?: SwarmTranslate;
  notifyFailure?: (notice: SwarmFailureNotice) => void;
  findConversation(conversationId: string): SwarmConversationReference | null;
  readTurnState(conversationId: string): TurnState | null;
  readOverlay(conversationId: string, turnId: string): TurnRecord | null;
  upsertOverlay(conversation: SwarmConversationReference, turn: TurnRecord): void;
  removeOverlay(conversation: SwarmConversationReference, turnId: string): void;
  hydrateConversation(
    conversation: SwarmConversationReference,
  ): PromiseLike<unknown> | null;
  attachAutoContinue(conversationId: string): void;
  reducePhase(frame: SwarmPushFrame, context: SwarmPresentationContext): void;
  reduceAgent(frame: SwarmPushFrame, context: SwarmPresentationContext): void;
  debug(message: string, detail?: unknown): void;
  warn(message: string, detail?: unknown): void;
}

export interface SwarmPresentationController {
  presentation: Readonly<{
    candidates(conversation: SwarmConversationReference): TurnRecord[];
    compose(
      conversation: SwarmConversationReference,
      durableState: TurnState,
      transientState: TurnState,
    ): TurnState;
    update(
      conversation: SwarmConversationReference,
      turnId: string,
      updateProjection: (
        projection: TurnProjection,
        overlay: TurnRecord,
      ) => boolean | void,
    ): TurnRecord | null;
    settle(conversation: SwarmConversationReference, turnId: string): void;
  }>;
  handleFrame(frame: SwarmPushFrame): void;
}

export interface SwarmPushRuntimePorts extends SwarmPushPresentationPorts {
  subscribe(handler: (frame: SwarmPushFrame) => void): void;
  unsubscribe(handler: (frame: SwarmPushFrame) => void): void;
}

export interface SwarmPushRuntime extends SwarmPresentationController {
  start(): void;
  destroy(): void;
}

const AGENT_FRAME_TYPES = new Set([
  'swarm_agent_phase',
  'swarm_agent_progress',
  'swarm_agent_complete',
  'swarm_agent_error',
  'swarm_agent_tool_call',
]);

/** Build the lifecycle controller while keeping retained runtime as ports only. */
export function createSwarmPushPresentationController(
  ports: SwarmPushPresentationPorts,
): SwarmPresentationController {
  const failureNotice = createSwarmFailureNotices(ports.translate ?? (key => key), ports.notifyFailure ?? (() => {}));
  const candidates = (conversation: SwarmConversationReference): TurnRecord[] => {
    const state = ports.readTurnState(conversation?.id);
    return state
      ? selectCandidates(conversation.id, state, ports.readOverlay)
      : [];
  };

  const composePresentation = (
    conversation: SwarmConversationReference,
    durableState: TurnState,
    transientState: TurnState,
  ): TurnState => {
    if (!conversation?.id || !durableState || !transientState) {
      return transientState;
    }
    return compose(
      conversation.id,
      durableState,
      transientState,
      ports.readOverlay,
    );
  };

  const update = (
    conversation: SwarmConversationReference,
    turnId: string,
    updateProjection: (
      projection: TurnProjection,
      overlay: TurnRecord,
    ) => boolean | void,
  ): TurnRecord | null => {
    if (!conversation || !turnId) {
      return null;
    }
    const durable = ports.readTurnState(conversation.id)?.turnsById?.[turnId];
    const previousOverlay = ports.readOverlay(conversation.id, turnId);
    const source = rebase(durable, previousOverlay)
      ?? previousOverlay
      ?? durable;
    if (!source) return null;
    const overlay = copyTurn(source, true);
    if (updateProjection(overlay.projection, overlay) === false) return null;
    rebindSegments(overlay.projection);
    ports.upsertOverlay(conversation, overlay);
    return overlay;
  };

  const settle = (
    conversation: SwarmConversationReference,
    turnId: string,
  ): void => {
    const hydration = ports.hydrateConversation(conversation);
    if (!hydration) return;
    Promise.resolve(hydration).catch((error: unknown) => {
      ports.warn(
        '[SwarmPush] authoritative hydration failed after terminal frame; retrying once:',
        error,
      );
      return ports.hydrateConversation(conversation);
    }).then(() => {
      ports.removeOverlay(conversation, turnId);
    }).catch((error: unknown) => {
      // A settled turn must never stay pinned behind its transient overlay:
      // the rebased swarm fields would render it as still-active forever.
      ports.warn(
        '[SwarmPush] hydration retry failed; dropping stale overlay:',
        error,
      );
      ports.removeOverlay(conversation, turnId);
    });
  };

  const findOwningTurn = (
    conversation: SwarmConversationReference,
    frame: SwarmPushFrame,
  ): TurnRecord | null => {
    const state = ports.readTurnState(conversation.id);
    const taskId = swarmFrameExecutionTaskId(frame);
    const turns = candidates(conversation).filter(turn => {
      if (frame.turnId && frame.turnId !== turn.turnId) return false;
      if (!taskId && !frame.attemptId) return true;
      if (turn.projection.toolRounds?.some(round => swarmFrameMatchesExecution(frame, round))) return true;
      return entityRecordValues(state?.attemptsById ?? {}).some(attempt =>
        attempt !== undefined && attempt.turnId === turn.turnId && (!taskId || attempt.taskId === taskId)
        && (!frame.attemptId || attempt.attemptId === frame.attemptId));
    });
    const swarmId = String(frame.swarmId ?? frame.swarm_id ?? '');
    const agentId = String(frame.agentId ?? '');
    if (frame.type === 'swarm_phase' && ['planning', 'spawning', 'spawn_more'].includes(frame.phase ?? '')) {
      // Resolve across all eligible Turns, not independently per Turn: two
      // individually unique tool rows still represent ambiguous ownership.
      const launch = swarmLaunchSelection(turns.flatMap(turn => turn.projection.toolRounds ?? []), frame);
      if (launch.kind === 'matched') return turns.find(turn =>
        turn.projection.toolRounds?.includes(launch.round as TurnToolRound)) ?? null;
      if (launch.kind !== 'absent') return null;
    }
    // A task/session ID can span several launch rosters. Resolve a current
    // owner before fencing settled history; one finished wave is not proof
    // that every child of that task has finished.
    const ownsFrame = (round: TurnToolRound): boolean => {
      if (swarmSettled(round) || round._swarmEndTime || !swarmFrameMatchesExecution(frame, round)) return false;
      if (swarmId && swarmRunId(round) !== swarmId) return false;
      if (swarmId && !agentId) return !swarmSettled(round)
        && Boolean(round._swarm || round.toolName === 'spawn_agents');
      if (agentId) return swarmRoundAgentIds(round).includes(agentId);
      return Boolean(round._swarm || round.toolName === 'spawn_agents') && !swarmSettled(round) && !round._swarmEndTime;
    };
    const existing = turns.flatMap(turn => (turn.projection.toolRounds ?? [])
      .filter(ownsFrame).map(() => turn));
    if (existing.length) return existing.length === 1 ? existing[0] : null;
    if (agentId) return null;
    const durableState = ports.readTurnState(conversation.id);
    if (swarmId && durableState && newestAssistantTurns(durableState).some((turn) =>
      (turn.projection.toolRounds ?? []).some((round) =>
        swarmSettled(round) && swarmRunId(round) === swarmId))) return null;
    if (frame.type === 'swarm_phase'
        && ['planning', 'spawning', 'spawn_more'].includes(frame.phase ?? '')) {
      // With no retained launch, only the current running producer proves
      // admission. Historical attempts belong to recovery of actual receipts,
      // not permission to fabricate a new panel after regeneration.
      const eligible = turns.filter(turn => turn.status === 'running' && taskId
        && entityRecordValues(state?.attemptsById ?? {}).some(attempt => attempt
          && attempt.turnId === turn.turnId && attempt.attemptId === turn.currentAttemptId
          && attempt.status === 'running' && attempt.taskId === taskId
          && (!frame.attemptId || attempt.attemptId === frame.attemptId)));
      return eligible.length === 1 ? eligible[0] : null;
    }
    return null;
  };

  const presentation = Object.freeze({
    candidates,
    compose: composePresentation,
    update,
    settle,
  });

  return Object.freeze({
    presentation,
    handleFrame(frame: SwarmPushFrame): void {
      try {
        if (!frame?.type) return;
        // Canonical task mirrors carry conversationId while taskId may be an
        // execution id on older servers. Never treat executionTaskId as routing.
        const conversationId = frame.conversationId || frame.convId || frame.taskId;
        if (!conversationId || conversationId === '*') return;
        if (frame.type === 'swarm_autocontinue_started') {
          ports.attachAutoContinue(conversationId);
          return;
        }
        const conversation = ports.findConversation(conversationId);
        if (!conversation) return;

        if (frame.type === 'swarm_message_receipt') {
          const turns = candidates(conversation);
          const sender = turns.flatMap(turn => turn.projection.toolRounds ?? [])
            .find(round => round.toolName === 'send_agent_message' && sameMessageReceipt(messageReceiptIdentity(round), frame));
          let content: unknown;
          try { content = typeof sender?.toolArgs === 'string' ? JSON.parse(sender.toolArgs).content : (sender?.toolArgs as Record<string, unknown> | undefined)?.content; } catch { /* Missing request body is explicit. */ }
          const receipt = { ...frame, ...(typeof frame.content !== 'string' && typeof content === 'string' ? { content } : {}) };
          for (const turn of turns) {
            update(conversation, turn.turnId, projection => applyMessageReceipt(projection, receipt));
          }
          return;
        }
        const sourceTurn = findOwningTurn(conversation, frame);
        if (!sourceTurn) return;
        const owningRound = (sourceTurn.projection.toolRounds ?? []).find(round =>
          !swarmSettled(round) && !round._swarmEndTime && swarmFrameMatchesExecution(frame, round)
          && (!(frame.swarmId || frame.swarm_id)
            || swarmRunId(round) === String(frame.swarmId || frame.swarm_id))
          && swarmRoundAgentIds(round).includes(String(frame.agentId ?? '')));
        const snapshot = owningRound?._swarmSnapshot as { agents?: Record<string, unknown>[] } | undefined;
        const previousAgents = [...(Array.isArray(snapshot?.agents) ? snapshot.agents : []),
          ...(Array.isArray(owningRound?._swarmAgents) ? owningRound._swarmAgents : [])];
        const previousAgent = previousAgents.find((agent: any) => agent.id === frame.agentId);
        const overlay = update(
          conversation,
          sourceTurn.turnId,
          (assistantProjection) => {
            const context: SwarmPresentationContext = {
              convId: conversationId,
              taskId: swarmFrameExecutionTaskId(frame),
              assistantProjection,
            };
            if (frame.type === 'swarm_phase') {
              ports.reducePhase(frame, context);
            } else if (AGENT_FRAME_TYPES.has(frame.type)) {
              ports.reduceAgent(frame, context);
            } else {
              return false;
            }
            return true;
          },
        );
        if (overlay && owningRound) {
          const round = overlay.projection.toolRounds?.find(candidate => sameSwarmRound(candidate, owningRound));
          failureNotice(conversationId, String(frame.swarmId ?? frame.swarm_id ?? swarmRunId(owningRound)) + ':' + String(owningRound.toolCallId ?? sourceTurn.turnId), frame,
            Array.isArray(round?._swarmAgents) ? round._swarmAgents : [], previousAgent);
        }
        if (overlay && frame.type === 'swarm_phase'
            && (frame.phase === 'complete' || frame.phase === 'error')) {
          settle(conversation, overlay.turnId);
        }
      } catch (error: unknown) {
        ports.debug(
          '[SwarmPush] handler error:',
          error,
        );
      }
    },
  });
}

/** Own the single Swarm push subscription around the presentation reducer. */
export function createSwarmPushRuntime(
  ports: SwarmPushRuntimePorts,
): SwarmPushRuntime {
  const controller = createSwarmPushPresentationController(ports);
  let started = false;

  const start = (): void => {
    if (started) return;
    started = true;
    ports.subscribe(controller.handleFrame);
  };

  const destroy = (): void => {
    if (!started) return;
    started = false;
    ports.unsubscribe(controller.handleFrame);
  };

  return Object.freeze({ ...controller, start, destroy });
}
