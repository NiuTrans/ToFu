/**
 * Live-attempt, projected live activity, waiting-placeholder and concluded
 * goal-run presentation. Reads authoritative state and explicit local display
 * ports.
 * Entry: liveAttemptPresentation, projectLiveActivity,
 * waitingPlaceholderRedundant, selectAutopilotRunNotices.
 * Dependencies: conversation-sync.generated, turn-store, transient-turn, types,
 * round-outline (action state) and tool-summary-identity. */
import {
  type TurnModelRoute,
  type TurnRecord,
} from '../../../api/conversation-sync.generated';
import {
  type TurnState,
} from '../../domain/turn-store';
import {
  transientTurnPresentation,
  type TransientTurnPresentation,
} from '../../domain/transient-turn';
import {
  type UnknownRecord,
  type AutopilotRunNoticeReason,
  type AutopilotRunNoticeBlockViewModel,
  type ConversationBlockViewModel,
  type ConversationPresentationState,
} from './types';

import { normalizeSwarmAgent, mergeSwarmAgentCards, swarmLaunchReceipt, swarmRoundIsActive } from '../swarm-agent-presentation';
import { toolBlockActivityState } from '../round-outline';
import { toolSummaryIdentity } from '../tool-summary-identity';

import { goalRunAffiliations } from '../goal-run-affiliation';

/** Only join active phases with explicit run AND producing execution identity. */
export function selectGoalProgress(
  state: TurnState,
  liveTurnId: string | null,
): ReadonlyMap<string, NonNullable<TransientTurnPresentation['goalProgress']>> {
  const result = new Map<string, NonNullable<TransientTurnPresentation['goalProgress']>>();
  const worker = liveTurnId ? state.turnsById[liveTurnId] : undefined;
  const iteration = worker?.projection.orchestration?.iteration;
  if (!worker || !worker.runId || iteration == null || String(iteration) === '') return result;
  const attempt = worker.currentAttemptId ? state.attemptsById[worker.currentAttemptId] : undefined;
  if (!attempt?.taskId) return result;
  const role = worker.actor === 'assistant' ? 'worker' : worker.actor;
  if (!['worker', 'planner', 'critic', 'virtual_user'].includes(role)) return result;
  // Swarm overlay has already fenced these rounds before composition. Read only
  // active rounds; a settled snapshot must never revive a worker label.
  const workers: NonNullable<TransientTurnPresentation['goalProgress']>['workers'][number][] = [];
  for (const round of worker.projection.toolRounds ?? []) {
    if (workers.length >= 8) break;
    if ((!round._swarm && round.toolName !== 'spawn_agents') || round._swarmEndTime
        || (round._swarmSnapshot as { settled?: boolean } | undefined)?.settled) continue;
    const snapshot = round._swarmSnapshot as { agents?: Record<string, unknown>[] } | undefined;
    const recovered = snapshot?.agents ?? swarmLaunchReceipt(round).agents ?? [];
    const agents = mergeSwarmAgentCards(round, recovered).map(normalizeSwarmAgent);
    if (!swarmRoundIsActive(round, agents)) continue;
    for (const agent of agents) {
      if (workers.length >= 8) break;
      if (!['running', 'pending'].includes(agent.status)) continue;
      const calls = Array.isArray(agent._toolCalls) ? agent._toolCalls : [];
      const latest = calls[calls.length - 1];
      workers.push({
        text: String(agent.name || agent.id).slice(0, 160), status: String(agent.status),
        phase: String(agent.phase || agent.status), waitReason: String(agent.waitReason || ''),
        ...(latest ? { latestTool: [latest.toolName || latest.tool, latest.argsBrief].filter(Boolean).join(' · ').slice(0, 500) } : {}),
        ...(typeof latest?.error === 'string' ? { toolError: latest.error.slice(0, 500) } : {}),
      });
    }
  }
  const context = { iteration: String(iteration), role, rootSummary: false, workers };
  result.set(worker.turnId, context);
  for (const id of state.laneOrder.main ?? []) {
    const root = state.turnsById[id];
    if (!root || root.turnId === worker.turnId || root.ordinal >= worker.ordinal
        || root.status !== 'running' || root.runId !== worker.runId
        || transientTurnPresentation(root)) continue;
    const rootAttempt = root.currentAttemptId ? state.attemptsById[root.currentAttemptId] : undefined;
    if (rootAttempt?.taskId === attempt.taskId && !rootAttempt.queueBinding) {
      result.set(root.turnId, { ...context, rootSummary: true,
        currentPhase: state.livePhase ? liveAttemptPresentation(state.livePhase as object, worker.status) : undefined,
      });
      break;
    }
  }
  return result;
}

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

/** The default "waiting" fallback: no phase frame says what the attempt does. */
function isBareWaiting(liveStatus: TransientTurnPresentation): boolean {
  return liveStatus.phase === 'waiting'
    && (!liveStatus.detailKey
      || liveStatus.detailKey === 'stream.phase.waitingWorkerStatus')
    && !liveStatus.detail && !liveStatus.model
    && !liveStatus.toolContext && !liveStatus.tools?.length;
}

type ToolActionBlock = Extract<ConversationBlockViewModel, { kind: 'tool' | 'program' }>;

function toolStartedAt(block: ToolActionBlock): number | undefined {
  const round = block.round as UnknownRecord | undefined;
  const value = Number(round?.tStart ?? (block.source as UnknownRecord).tStart);
  return Number.isFinite(value) && value > 0 ? value : undefined;
}

/**
 * Fill the live row from the running Turn's own projection where its phase
 * frame says less. Semantic deltas clear the phase and one `tool_exec` frame
 * covers a whole batch, while a grouped reply renders reasoning above its tool
 * panel and receipts below its prose, so this row is often the only live
 * surface in view (rationale: docs/RENDER_CONTRACT.md). Calls this projection
 * marks active name the work and carry the earliest backend start for elapsed
 * time; unsettled reasoning reads as the client-derived `thinking_active`.
 * Every other reported phase stays as sent.
 */
export function projectLiveActivity(
  liveStatus: TransientTurnPresentation,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): void {
  if (liveStatus.goalProgress) return;
  const bare = isBareWaiting(liveStatus);
  if (bare || liveStatus.phase === 'tool_exec') {
    const active = blocks.filter((block): block is ToolActionBlock => (
      (block.kind === 'tool' || block.kind === 'program')
      && toolBlockActivityState(block) === 'active'));
    const identities = active.map(toolSummaryIdentity);
    const names = [...new Set(identities.map(identity => identity.name).filter(Boolean))];
    if (names.length) {
      const only = identities.length === 1 ? identities[0]! : null;
      const intent = only ? (only.intent !== only.name ? only.intent : only.target) : '';
      liveStatus.phase = 'tool_exec';
      liveStatus.tools = names;
      liveStatus.detailKey = intent ? 'stream.phase.runningToolIntent' : 'stream.phase.runningTools';
      liveStatus.detailArgs = intent ? { tool: names[0]!, intent } : { tools: names.join(', ') };
      const starts = active.map(toolStartedAt).filter((value): value is number => value != null);
      if (starts.length) liveStatus.startedAt = Math.min(...starts);
      return;
    }
  }
  if (bare && blocks.some(block => block.kind === 'thinking' && !block.terminal)) {
    liveStatus.phase = 'thinking_active';
    delete liveStatus.detailKey;
  }
}

/**
 * True when the row would only restate a streaming block rendered directly
 * above it: a bare "waiting" placeholder under a still-streaming answer tail,
 * or projected reasoning status when the reasoning disclosure is the reply's
 * last part (no call or prose renders between them).
 */
export function waitingPlaceholderRedundant(
  liveStatus: TransientTurnPresentation,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): boolean {
  if (liveStatus.goalProgress) return false;
  const tail = blocks[blocks.length - 1];
  if (!tail) return false;
  if (liveStatus.phase === 'thinking_active') {
    return tail.kind === 'thinking' && !blocks.some(block => (
      block.kind === 'tool' || block.kind === 'program' || block.kind === 'text'));
  }
  if (!isBareWaiting(liveStatus)) return false;
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
  const summaries = presentation.autopilotSummaries ?? {};
  const turns = (state.laneOrder.main ?? []).flatMap((turnId) => {
    const turn = state.turnsById[turnId];
    return turn ? [turn] : [];
  });
  const affiliations = goalRunAffiliations(turns);
  const lastTurnByRun = new Map<string, TurnRecord>();
  // Conclusions follow the whole affiliated run. Restart evidence belongs
  // to the last explicitly stamped phase, even before a successor starts.
  const lastExplicitGoalTurn = new Map<string, TurnRecord>();
  for (const turn of turns) {
    const runId = affiliations.get(turn.turnId);
    if (runId) lastTurnByRun.set(runId, turn);
    const goalRunId = turn.projection.orchestration?.goalRunId;
    if (goalRunId) lastExplicitGoalTurn.set(goalRunId, turn);
  }
  const noticesByTurn = new Map<string, AutopilotRunNoticeBlockViewModel[]>();
  for (const [runId, turn] of lastExplicitGoalTurn) {
    if (turn.status !== 'interrupted' || turn.settlement?.cause !== 'server_restart'
        || summaries[runId]?.status === 'concluded') continue;
    noticesByTurn.set(turn.turnId, [{
      blockId: `${turn.turnId}:autopilot-run-notice:${runId}`,
      kind: 'autopilot-run-notice', identitySource: 'contract', source: turn,
      value: { runId, reason: 'worker_lost', unsent: false,
        canRequestRecovery: turn === turns[turns.length - 1] },
    }]);
  }
  for (const [runId, target] of lastTurnByRun) {
    const source = summaries[runId];
    if (!source || typeof source !== 'object' || Array.isArray(source)
        || source.status !== 'concluded') continue;
    const reason = autopilotNoticeReason(source.reason);
    if (!reason) continue;
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
