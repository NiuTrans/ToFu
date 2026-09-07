/**
 * Compose conversation-scoped swarm telemetry over authoritative Turn state.
 *
 * Swarm push frames are lifecycle presentation, not durable transcript facts.
 * The overlay therefore owns only swarm-specific round fields and is rebased
 * onto the newest TurnStore revision before every update and Surface render.
 */
import type {
  TurnContentSegment,
  TurnProjection,
  TurnRecord,
  TurnToolRound,
} from '../../api/conversation-sync.generated';
import type { TurnState } from '../domain/turn-store';

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
  '_swarmAgents',
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
    Boolean(round && (round._swarm || round.toolName === 'spawn_agents')));
}

function sameSwarmRound(left: TurnToolRound, right: TurnToolRound): boolean {
  if (left.toolCallId && right.toolCallId) {
    return left.toolCallId === right.toolCallId;
  }
  const leftIsSwarm = Boolean(left._swarm || left.toolName === 'spawn_agents');
  const rightIsSwarm = Boolean(right._swarm || right.toolName === 'spawn_agents');
  if (!leftIsSwarm || !rightIsSwarm) return false;
  if (left.roundNum != null && right.roundNum != null) {
    return Number(left.roundNum) === Number(right.roundNum);
  }
  return left.llmRound != null && right.llmRound != null
    && Number(left.llmRound) === Number(right.llmRound);
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
  const roundsByCallId = new Map<string, TurnToolRound>();
  for (const round of mutable.toolRounds) {
    const callId = String(round?.toolCallId ?? '');
    if (callId && !roundsByCallId.has(callId)) {
      roundsByCallId.set(callId, round);
    }
  }
  mutable.segments = mutable.segments.map((segment) => {
    if (!segment || segment.type !== 'tool_use') return segment;
    const callId = String(segment.id ?? '');
    // llmRound is attempt-local and repeats after continue/regenerate. A
    // durable call ID is therefore exclusive authority; round fallback exists
    // only for id-less legacy data and only when its scoped match is unique.
    const round = callId
      ? roundsByCallId.get(callId)
      : uniqueLegacyRound(segment, mutable.toolRounds ?? []);
    return round ? { ...segment, _round: round } : segment;
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

  for (const [previousIndex, previousRound] of (
    previousProjection.toolRounds ?? []
  ).entries()) {
    if (!previousRound
        || !(previousRound._swarm || previousRound.toolName === 'spawn_agents')) {
      continue;
    }
    let targetIndex = projection.toolRounds?.findIndex(
      (candidate) => sameSwarmRound(candidate, previousRound),
    ) ?? -1;
    if (targetIndex < 0) {
      // A push can narrowly precede the authoritative spawning-tool revision.
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
    if (!target) continue;
    const merged: TurnToolRound = { ...target };
    const previousFields = previousRound as Record<string, unknown>;
    const mergedFields = merged as Record<string, unknown>;
    for (const field of SWARM_ROUND_PRESENTATION_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(previousFields, field)) {
        mergedFields[field] = cloneValue(previousFields[field]);
      }
    }
    if (projection.toolRounds) projection.toolRounds[targetIndex] = merged;
  }
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
  return newestAssistantTurns(state).map((durable) => rebase(
    durable,
    readOverlay(conversationId, durable.turnId),
  ) ?? durable);
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
    if (!overlay || !hasSwarm(overlay)) continue;
    if (!changed) turnsById = { ...turnsById };
    turnsById[durable.turnId] = rebase(durable, overlay) ?? durable;
    changed = true;
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

export interface SwarmConversationReference {
  id: string;
  [key: string]: unknown;
}

export interface SwarmPushFrame {
  type: string;
  convId?: string;
  taskId?: string;
  phase?: string;
  [key: string]: unknown;
}

export interface SwarmPresentationContext {
  convId: string;
  taskId: string;
  assistantProjection: TurnProjection;
}

export interface SwarmPushPresentationPorts {
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
    const turns = candidates(conversation);
    const existing = turns.find((turn) => hasSwarm(turn));
    if (existing) return existing;
    if (frame.type === 'swarm_phase'
        && ['planning', 'spawning', 'spawn_more'].includes(frame.phase ?? '')) {
      return turns.find((turn) =>
        turn.status === 'running' || turn.status === 'completed') ?? null;
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
        const conversationId = frame.convId || frame.taskId;
        if (!conversationId || conversationId === '*') return;
        if (frame.type === 'swarm_autocontinue_started') {
          ports.attachAutoContinue(conversationId);
          return;
        }
        const conversation = ports.findConversation(conversationId);
        if (!conversation) return;
        const sourceTurn = findOwningTurn(conversation, frame);
        if (!sourceTurn) return;
        const overlay = update(
          conversation,
          sourceTurn.turnId,
          (assistantProjection) => {
            const context: SwarmPresentationContext = {
              convId: conversationId,
              taskId: conversationId,
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
