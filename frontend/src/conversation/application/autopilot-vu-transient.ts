/**
 * Pure reducer for the browser-only virtual-user turn shown while Autopilot
 * is producing a durable backend Turn.
 *
 * Raw task frames enter here once.  The reducer emits an immutable typed Turn
 * overlay with stable block identities; it owns no DOM, persistence, cache,
 * transport, or global conversation array.
 */
import type {
  TurnContentSegment,
  TurnProjection,
  TurnToolRound,
} from '../../api/conversation-sync.generated';
import type {
  TransientTurnPresentation,
  TransientTurnRecord,
} from '../domain/transient-turn';

type UnknownRecord = Record<string, unknown>;

export interface AutopilotVuLifecycleEvent extends UnknownRecord {
  type: string;
  vuMsgId: string;
  replaySnapshot?: UnknownRecord;
  inner?: UnknownRecord;
  vuMessage?: UnknownRecord;
}

export interface CreateAutopilotVuTransientInput {
  conversationId: string;
  vuMsgId: string;
  runId?: string;
  timestamp?: number;
  replaySnapshot?: UnknownRecord;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function number(value: unknown): number | undefined {
  if (value == null || value === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : {};
}

function records(value: unknown): UnknownRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is UnknownRecord => (
      Boolean(item) && typeof item === 'object' && !Array.isArray(item)
    )).map((item) => ({ ...item }))
    : [];
}

export function autopilotVuTransientTurnId(vuMsgId: string): string {
  return `transient:autopilot-vu:${vuMsgId}`;
}

export function maskAutopilotVuMachineTokens(value: string): string {
  return value.split('\n').filter((line) => (
    !/^\s*\[VU:\s*TASK_DONE\]\s*$/.test(line)
      && !/^\s*\[PROGRESS:\s*resolved\s*=\s*\d+\s*(?:,|;|\s)\s*remaining\s*=\s*\d+\s*\]\s*$/.test(line)
  )).join('\n');
}

function toolIdentity(round: UnknownRecord, index: number): string {
  return text(round.toolCallId)
    || `round-${number(round.roundNum) ?? index + 1}`;
}

function toolResult(round: UnknownRecord): UnknownRecord {
  const status = text(round.status) || 'running';
  const content = round.toolContent ?? round.results ?? round._partialOutput ?? null;
  return {
    status,
    ...(content == null ? {} : { content }),
    ...(record(round.result).status ? record(round.result) : {}),
  };
}

function projectionSegments(
  content: string,
  thinking: string,
  toolRounds: ReadonlyArray<UnknownRecord>,
): TurnContentSegment[] {
  const segments: TurnContentSegment[] = [];
  if (thinking) {
    segments.push({
      type: 'thinking',
      blockId: 'thinking:autopilot-live',
      text: thinking,
      terminal: false,
    });
  }
  toolRounds.forEach((round, index) => {
    const identity = toolIdentity(round, index);
    segments.push({
      type: 'tool_use',
      blockId: `tool:${identity}`,
      id: identity,
      name: text(round.toolName) || text(round.name) || 'tool',
      input: round.toolArgs ?? (round.query ? { query: round.query } : {}),
      result: toolResult(round),
      ...(number(round.llmRound) == null
        ? {} : { llmRound: number(round.llmRound) }),
      _round: round as TurnToolRound,
    });
  });
  if (content) {
    segments.push({
      type: 'text',
      blockId: 'text:autopilot-live',
      text: content,
      deliverable: true,
      terminal: false,
    });
  }
  return segments;
}

function presentation(
  phase: string,
  values: Partial<TransientTurnPresentation> = {},
): TransientTurnPresentation {
  return {
    kind: 'autopilot-virtual-user',
    phase,
    label: values.label || 'Autopilot is starting…',
    detail: values.detail || '',
    ...(values.detailKey ? { detailKey: values.detailKey } : {}),
    ...(values.detailArgs ? { detailArgs: values.detailArgs } : {}),
    ...(values.tools ? { tools: values.tools } : {}),
    ...(values.toolContext ? { toolContext: values.toolContext } : {}),
    ...(values.toolContextTools
      ? { toolContextTools: values.toolContextTools } : {}),
    ...(values.attempt == null ? {} : { attempt: values.attempt }),
    ...(values.statusCode == null ? {} : { statusCode: values.statusCode }),
    ...(values.model ? { model: values.model } : {}),
    ...(values.thinkingLength == null
      ? {} : { thinkingLength: values.thinkingLength }),
  };
}

function withProjection(
  turn: TransientTurnRecord,
  projection: TurnProjection,
  transientPresentation: TransientTurnPresentation,
  timestamp: number,
): TransientTurnRecord {
  return {
    ...turn,
    projection,
    transientPresentation,
    projectionRevision: turn.projectionRevision + 1,
    updatedAt: timestamp,
  };
}

function snapshotProjection(snapshot: UnknownRecord): TurnProjection {
  const content = maskAutopilotVuMachineTokens(text(snapshot.content));
  const thinking = text(snapshot.thinking);
  const toolRounds = records(snapshot.toolRounds);
  return {
    content,
    thinking,
    toolRounds: toolRounds as TurnToolRound[],
    segments: projectionSegments(content, thinking, toolRounds),
    _isVirtualUser: true,
    timestamp: number(snapshot.timestamp) ?? Date.now(),
  };
}

export function createAutopilotVuTransientTurn(
  input: CreateAutopilotVuTransientInput,
): TransientTurnRecord {
  const timestamp = input.timestamp ?? Date.now();
  const projection = snapshotProjection(input.replaySnapshot ?? {});
  projection.timestamp = timestamp;
  return {
    turnId: autopilotVuTransientTurnId(input.vuMsgId),
    conversationId: input.conversationId,
    laneId: 'main',
    parentTurnId: null,
    ordinal: Number.MAX_SAFE_INTEGER,
    actor: 'virtual_user',
    kind: 'autopilot_virtual_user',
    runId: input.runId || input.vuMsgId,
    status: 'running',
    currentAttemptId: null,
    projection,
    projectionRevision: 1,
    settlement: {},
    transientPresentation: presentation('warming'),
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

function matchingToolRoundIndex(
  rounds: ReadonlyArray<UnknownRecord>,
  inner: UnknownRecord,
): number {
  const callId = text(inner.toolCallId);
  const roundNum = number(inner.roundNum);
  if (callId) {
    const exact = rounds.findIndex((round) => text(round.toolCallId) === callId);
    if (exact >= 0) return exact;
  }
  return rounds.findIndex((round) => (
    roundNum != null && number(round.roundNum) === roundNum
  ));
}

function phasePresentation(
  current: TransientTurnPresentation,
  inner: UnknownRecord,
): TransientTurnPresentation {
  const phase = text(inner.phase) || 'working';
  const args = record(inner.detailArgs);
  return presentation(phase, {
    ...current,
    label: text(inner.detail) || current.label,
    detail: text(inner.detail),
    detailKey: text(inner.detailKey),
    detailArgs: Object.fromEntries(Object.entries(args).flatMap(([key, value]) => (
      typeof value === 'string' || typeof value === 'number'
        ? [[key, value]] : []
    ))),
    tools: Array.isArray(inner.tools)
      ? inner.tools.filter((item): item is string => typeof item === 'string')
      : [],
    toolContext: text(inner.toolContext),
    toolContextTools: Array.isArray(inner.toolContextTools)
      ? inner.toolContextTools.filter(
        (item): item is string => typeof item === 'string',
      ) : [],
    attempt: number(inner.attempt) ?? 0,
    statusCode: number(inner.statusCode) ?? 0,
    model: text(inner.model),
    thinkingLength: 0,
  });
}

export function reduceAutopilotVuTransientTurn(
  current: TransientTurnRecord,
  event: AutopilotVuLifecycleEvent,
  timestamp = Date.now(),
): TransientTurnRecord {
  if (event.type === 'autopilot_vu_start' && event.replaySnapshot) {
    const projection = snapshotProjection(event.replaySnapshot);
    return withProjection(
      current,
      projection,
      presentation('warming', current.transientPresentation),
      timestamp,
    );
  }
  if (event.type !== 'autopilot_vu_event') return current;
  const inner = record(event.inner);
  const type = text(inner.type);
  const previousProjection = current.projection;
  let content = text(previousProjection.content);
  let thinking = text(previousProjection.thinking);
  let rounds = records(previousProjection.toolRounds);
  let nextPresentation = current.transientPresentation
    ?? presentation('warming');

  if (type === 'delta') {
    const contentDelta = text(inner.content);
    const thinkingDelta = text(inner.thinking);
    if (contentDelta) content = maskAutopilotVuMachineTokens(content + contentDelta);
    if (thinkingDelta) thinking += thinkingDelta;
    const thinkingLength = contentDelta
      ? 0 : (nextPresentation.thinkingLength ?? 0) + thinkingDelta.length;
    nextPresentation = presentation(
      contentDelta ? 'responding' : 'thinking_active',
      {
        ...nextPresentation,
        label: contentDelta ? 'Autopilot is responding…' : 'Autopilot is reasoning…',
        thinkingLength,
      },
    );
  } else if (type === 'tool_start') {
    const candidate: UnknownRecord = {
      roundNum: number(inner.roundNum),
      query: text(inner.query),
      results: null,
      status: 'searching',
      toolName: text(inner.toolName),
      toolCallId: text(inner.toolCallId),
      toolArgs: inner.toolArgs ?? null,
      llmRound: number(inner.llmRound) ?? null,
    };
    const match = matchingToolRoundIndex(rounds, inner);
    rounds = match >= 0
      ? rounds.map((round, index) => index === match ? { ...round, ...candidate } : round)
      : [...rounds, candidate];
    nextPresentation = presentation('tool_exec', {
      ...nextPresentation,
      label: text(inner.query) || text(inner.toolName) || 'Autopilot is using a tool…',
      detail: text(inner.query) || text(inner.toolName),
      tools: text(inner.toolName) ? [text(inner.toolName)] : [],
    });
  } else if (['tool_result', 'tool_progress', 'tool_complete', 'tool_compacted'].includes(type)) {
    const match = matchingToolRoundIndex(rounds, inner);
    if (match >= 0) {
      rounds = rounds.map((round, index) => {
        if (index !== match) return round;
        const next = { ...round };
        if (type === 'tool_result') {
          next.results = inner.results;
          next.status = text(inner.status) || 'done';
          for (const key of ['searchDiag', 'cacheSource', 'engineBreakdown',
            'toolSearchTotal', 'toolSearchNextCursor', 'toolSearchFailOpen',
            'rejection', '_rejected']) {
            if (Object.prototype.hasOwnProperty.call(inner, key)) next[key] = inner[key];
          }
        } else if (type === 'tool_progress') {
          next._partialOutput = text(next._partialOutput) + text(inner.chunk);
          if (inner.grepSearchIntercepted === true) next.grepSearchIntercepted = true;
        } else if (type === 'tool_complete') {
          next.toolContent = inner.toolContent ?? null;
          next.status = text(inner.status) || 'done';
          if (inner.toolTokens != null) next.toolTokens = inner.toolTokens;
          for (const key of ['rejection', '_rejected']) {
            if (Object.prototype.hasOwnProperty.call(inner, key)) next[key] = inner[key];
          }
        } else {
          next.compactionLayer = text(inner.compactionLayer)
            || text(next.compactionLayer) || 'L1';
          for (const key of ['compactedFromChars', 'compactedToChars', 'toolTokens']) {
            if (inner[key] != null) next[key] = inner[key];
          }
        }
        return next;
      });
    }
  } else if (type === 'phase') {
    nextPresentation = phasePresentation(nextPresentation, inner);
  } else {
    return current;
  }

  const projection: TurnProjection = {
    ...previousProjection,
    content,
    thinking,
    toolRounds: rounds as TurnToolRound[],
    segments: projectionSegments(content, thinking, rounds),
    timestamp,
  };
  return withProjection(current, projection, nextPresentation, timestamp);
}

export function settleAutopilotVuTransientTurn(
  current: TransientTurnRecord | null,
  conversationId: string,
  vuMsgId: string,
  value: unknown,
  timestamp = Date.now(),
): TransientTurnRecord {
  const finalMessage = record(value);
  const projection = snapshotProjection(finalMessage);
  const backendSegments = Array.isArray(finalMessage.segments)
    ? finalMessage.segments as TurnContentSegment[] : [];
  if (backendSegments.length) projection.segments = backendSegments;
  projection.timestamp = number(finalMessage.timestamp) ?? timestamp;
  const base = current ?? createAutopilotVuTransientTurn({
    conversationId, vuMsgId, timestamp,
  });
  return {
    ...base,
    turnId: text(finalMessage._turnId) || base.turnId,
    runId: text(finalMessage._autopilotRunId) || base.runId,
    status: 'completed',
    projection,
    projectionRevision: Math.max(base.projectionRevision + 1,
      number(finalMessage._projectionRevision) ?? 1),
    settlement: { outcome: 'completed', cause: 'autopilot_virtual_user' },
    transientPresentation: undefined,
    updatedAt: timestamp,
  };
}
