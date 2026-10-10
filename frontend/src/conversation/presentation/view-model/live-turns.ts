/**
 * Live-attempt, waiting-placeholder and concluded goal-run presentation. Reads
 * authoritative state and explicit local display ports.
 * Entry: liveAttemptPresentation, waitingPlaceholderRedundant,
 * selectAutopilotRunNotices.
 * Dependencies: conversation-sync.generated, turn-store, transient-turn, types. */
import {
  type TurnModelRoute,
  type TurnRecord,
} from '../../../api/conversation-sync.generated';
import {
  type TurnState,
} from '../../domain/turn-store';
import {
  type TransientTurnPresentation,
} from '../../domain/transient-turn';
import {
  type UnknownRecord,
  type AutopilotRunNoticeReason,
  type AutopilotRunNoticeBlockViewModel,
  type ConversationBlockViewModel,
  type ConversationPresentationState,
} from './types';

const AUTOPILOT_NOTICE_REASONS = new Set<AutopilotRunNoticeReason>([
  'yielded_to_human',
  'aborted_mid_vu',
  'superseded',
  'budget_exhausted',
  'no_progress',
  'stuck',
  'stalled',
  'vu_blocked',
]);

function autopilotNoticeReason(value: unknown): AutopilotRunNoticeReason | null {
  return typeof value === 'string'
    && AUTOPILOT_NOTICE_REASONS.has(value as AutopilotRunNoticeReason)
    ? value as AutopilotRunNoticeReason : null;
}

export function liveAttemptPresentation(
  value: unknown,
  durableStatus: TurnRecord['status'],
): TransientTurnPresentation {
  const rawPhase = value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : {};
  // A committed running Turn proves physical entry even if the following
  // worker-start phase could not persist. An older queue frame is then stale.
  const phase = durableStatus === 'running' && rawPhase.phase === 'executor_queued'
    ? {} : rawPhase;
  const stringValue = (field: string): string => (
    typeof phase[field] === 'string' ? phase[field] as string : ''
  );
  const numberValue = (field: string): number | undefined => {
    const parsed = Number(phase[field]);
    return Number.isFinite(parsed) ? parsed : undefined;
  };
  const stringArray = (field: string): string[] | undefined => (
    Array.isArray(phase[field])
      ? (phase[field] as unknown[]).filter(
        (item): item is string => typeof item === 'string',
      ) : undefined
  );
  const detailArgs = phase.detailArgs && typeof phase.detailArgs === 'object'
      && !Array.isArray(phase.detailArgs)
    ? Object.fromEntries(Object.entries(phase.detailArgs as UnknownRecord)
      .filter((entry): entry is [string, string | number] => (
        typeof entry[1] === 'string' || typeof entry[1] === 'number'
      ))) : undefined;
  const rawModelRoute = phase.modelRoute && typeof phase.modelRoute === 'object'
      && !Array.isArray(phase.modelRoute)
    ? phase.modelRoute as UnknownRecord : undefined;
  const modelRoute = rawModelRoute
      && typeof rawModelRoute.selectedModel === 'string'
      && typeof rawModelRoute.resolvedModel === 'string'
      && typeof rawModelRoute.role === 'string'
      && (rawModelRoute.tier === 'light'
        || rawModelRoute.tier === 'standard'
        || rawModelRoute.tier === 'heavy')
      && (rawModelRoute.kind === 'selected'
        || rawModelRoute.kind === 'role_tier')
    ? rawModelRoute as TurnModelRoute : undefined;
  const reportedPhase = stringValue('phase');
  // Binding a task precedes physical worker entry and its durable start
  // commit. Only the executor's explicit phase proves queue residence.
  const fallbackPhase = durableStatus === 'pending'
    ? 'executor_preparing'
    : 'waiting';
  const fallbackDetailKey = durableStatus === 'pending'
    ? 'stream.phase.executorPreparing'
    : 'stream.phase.waitingWorkerStatus';
  return {
    kind: 'attempt',
    phase: reportedPhase || fallbackPhase,
    ...(numberValue('seq') == null ? {} : { seq: numberValue('seq') }),
    label: stringValue('label'),
    detail: stringValue('detail'),
    ...(stringValue('detailKey')
      ? { detailKey: stringValue('detailKey') }
      : (reportedPhase ? {} : { detailKey: fallbackDetailKey })),
    ...(detailArgs && Object.keys(detailArgs).length ? { detailArgs } : {}),
    ...(stringArray('tools')?.length ? { tools: stringArray('tools') } : {}),
    ...(stringValue('toolContext')
      ? { toolContext: stringValue('toolContext') } : {}),
    ...(stringArray('toolContextTools')?.length
      ? { toolContextTools: stringArray('toolContextTools') } : {}),
    ...(numberValue('attempt') == null
      ? {} : { attempt: numberValue('attempt') }),
    ...(numberValue('statusCode') == null
      ? {} : { statusCode: numberValue('statusCode') }),
    ...(stringValue('model') ? { model: stringValue('model') } : {}),
    ...(modelRoute ? { modelRoute } : {}),
    ...(numberValue('thinkingLength') == null
      ? {} : { thinkingLength: numberValue('thinkingLength') }),
  };
}

/**
 * True when the live-status row is the bare default "waiting" placeholder
 * (no phase frame has arrived from the attempt) AND a thinking/answer block
 * is still streaming at the turn tail — in that state the streaming block
 * itself is the honest live surface, so the placeholder is pure noise.
 */
export function waitingPlaceholderRedundant(
  liveStatus: TransientTurnPresentation,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): boolean {
  const isBareWaiting = liveStatus.phase === 'waiting'
    && (!liveStatus.detailKey
      || liveStatus.detailKey === 'stream.phase.waitingWorkerStatus');
  if (!isBareWaiting) return false;
  if (liveStatus.detail || liveStatus.model
      || liveStatus.toolContext || liveStatus.tools?.length) return false;
  const tail = blocks[blocks.length - 1];
  if (!tail) return false;
  if (tail.kind !== 'thinking' && tail.kind !== 'text') return false;
  return !tail.terminal;
}

/**
 * Join backend run conclusions to the last Turn they describe.
 *
 * `_autopilotRunId` identifies persisted virtual-user participants. The run
 * tail also owns following non-human assistant Turns until a real human Turn
 * or another explicitly stamped run starts. This replaces the old DOM sibling
 * scan with a pure, stable-id join over the authoritative main lane.
 */
export function selectAutopilotRunNotices(
  state: TurnState,
  presentation: ConversationPresentationState,
): ReadonlyMap<string, ReadonlyArray<AutopilotRunNoticeBlockViewModel>> {
  const summaries = presentation.autopilotSummaries;
  if (!summaries) return new Map();
  const turns = (state.laneOrder.main ?? []).flatMap((turnId) => {
    const turn = state.turnsById[turnId];
    return turn ? [turn] : [];
  });
  const lastStampedIndexByRun = new Map<string, number>();
  turns.forEach((turn, index) => {
    const runId = turn.projection._autopilotRunId;
    if (runId) lastStampedIndexByRun.set(runId, index);
  });
  const noticesByTurn = new Map<string, AutopilotRunNoticeBlockViewModel[]>();
  for (const [runId, stampedIndex] of lastStampedIndexByRun) {
    const source = summaries[runId];
    if (!source || typeof source !== 'object' || Array.isArray(source)
        || source.status !== 'concluded') continue;
    const reason = autopilotNoticeReason(source.reason);
    if (!reason) continue;
    let targetIndex = stampedIndex;
    for (let index = stampedIndex + 1; index < turns.length; index += 1) {
      const candidate = turns[index];
      if (candidate.actor === 'human') break;
      const candidateRunId = candidate.projection._autopilotRunId;
      if (candidateRunId && candidateRunId !== runId) break;
      targetIndex = index;
    }
    const target = turns[targetIndex];
    if (!target) continue;
    const content = source.content == null ? '' : String(source.content);
    const notice: AutopilotRunNoticeBlockViewModel = {
      blockId: `${target.turnId}:autopilot-run-notice:${runId}`,
      kind: 'autopilot-run-notice',
      identitySource: 'contract',
      source,
      value: {
        runId,
        reason,
        unsent: Boolean(source.unsent && content),
        ...(content ? { content } : {}),
        ...(source.hasFullReport ? { hasFullReport: true } : {}),
      },
    };
    const existing = noticesByTurn.get(target.turnId) ?? [];
    existing.push(notice);
    noticesByTurn.set(target.turnId, existing);
  }
  return noticesByTurn;
}
