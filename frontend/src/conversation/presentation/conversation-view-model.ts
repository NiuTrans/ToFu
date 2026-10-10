/**
 * Pure ConversationViewModel composition over immutable TurnStore. Owns lane/attempt
 * joins and Turn memoization; delegates content projection to view-model/.
 * Entry: selectConversationViewModel.
 * Dependencies: background-command-completion, conversation-sync.generated, turn-store, transient-turn, turn-finish, types, turn-blocks, turn-metadata, live-turns.
 */
import type { AutomationManagementPresentation } from '../../api/automation.generated';
import {
  createBackgroundCommandCompletionProjection,
} from './background-command-completion';
import {
  type BranchLaneDescriptor,
  type TurnRecord,
} from '../../api/conversation-sync.generated';
import {
  type TurnState,
} from '../domain/turn-store';
import {
  transientTurnPresentation,
} from '../domain/transient-turn';
import {
  presentTurnFinish,
} from './turn-finish';
import {
  type ConversationBlockViewModel,
  type ConversationPresentationState,
  type TranslationActivity,
  type ConversationTurnViewModel,
  type ConversationLaneViewModel,
  type ConversationViewModel,
  type ConversationViewModelDiagnostics,
} from './view-model/types';
import {
  selectTurnBlocks,
} from './view-model/turn-blocks';
import {
  metadataFrom,
  actionsFor,
} from './view-model/turn-metadata';
import {
  liveAttemptPresentation,
  waitingPlaceholderRedundant,
  selectAutopilotRunNotices,
} from './view-model/live-turns';

export type {
  ConversationBlockKind,
  BlockIdentitySource,
  TextBlockViewModel,
  ThinkingBlockViewModel,
  ToolBlockViewModel,
  ProgramBlockViewModel,
  AttachmentsBlockViewModel,
  InjectionsBlockViewModel,
  SystemNoteBlockViewModel,
  SystemNoteGroupBlockViewModel,
  FileChangesBlockViewModel,
  ProvenanceBlockViewModel,
  OriginBlockViewModel,
  ContextBlockViewModel,
  CompactionBlockViewModel,
  RolledBackBlockViewModel,
  ImageGenerationBlockViewModel,
  ProposedPlanBlockViewModel,
  PlanExecutionBlockViewModel,
  ConversationArtifactViewModel,
  ArtifactsBlockViewModel,
  AutopilotRunNoticeReason,
  AutopilotRunNotice,
  AutopilotRunNoticeBlockViewModel,
  ActivityEventBlockViewModel,
  LiveStatusBlockViewModel,
  QueueStatusBlockViewModel,
  ConversationBlockViewModel,
  TurnMetadataViewModel,
  TranslationDisplayMode,
  ConversationPresentationState,
  TranslationActivity,
  ConversationTurnViewModel,
  ConversationTurnAction,
  ConversationTurnActionViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationViewModel,
  PlanDecisionViewModel,
  ConversationViewModelDiagnostics,
} from './view-model/types';
export { selectTurnBlocks } from './view-model/turn-blocks';

function roleFor(turn: TurnRecord): 'user' | 'assistant' {
  return turn.actor === 'human' || turn.actor === 'critic'
    || turn.actor === 'virtual_user' ? 'user' : 'assistant';
}

interface LaneOwner {
  parentTurnId: string;
  descriptor?: BranchLaneDescriptor;
  order: number;
}

/* A "plain" Turn (no branch lanes, no additive blocks, not the live Turn) is a
 * pure function of its own Turn object plus a handful of per-Turn presentation
 * scalars.  Memoizing the whole view-model object on that key lets a long
 * settled transcript return identical Turn references across frames, so the
 * keyed Surface can skip the entire Turn (not just its blocks).  Anything with
 * a cross-Turn or live input is deliberately left uncached. */
interface TurnVmMemoEntry {
  taskTimePresentations: ReadonlyArray<AutomationManagementPresentation> | undefined;
  laneId: string;
  taskId: string;
  commandPending: string;
  translationMode: string;
  costSignature: string;
  inspector: boolean;
  translationActivity: TranslationActivity | undefined;
  vm: ConversationTurnViewModel;
}

const turnVmMemo = new WeakMap<TurnRecord, TurnVmMemoEntry>();

const backgroundCommands = createBackgroundCommandCompletionProjection();

/** Select the complete lane tree without relying on message array positions. */
export function selectConversationViewModel(
  state: TurnState,
  diagnostics: ConversationViewModelDiagnostics = {},
  presentation: ConversationPresentationState = {},
): ConversationViewModel {

  backgroundCommands.update(state);
  const autopilotNoticesByTurn = selectAutopilotRunNotices(state, presentation);
  const nativeQueueByInputTurn = new Map(
    state.queueItems.flatMap((item) => (
      item.inputTurnId ? [[item.inputTurnId, item] as const] : []
    )),
  );
  const queuedOutputTurnIds = new Set(
    state.queueItems.flatMap((item) => item.outputTurnId ? [item.outputTurnId] : []),
  );
  const latestAttemptByTurn = new Map<string, {
    taskId: string;
    createdAt: number;
  }>();
  for (const attempt of Object.values(state.attemptsById)) {
    if (!attempt?.turnId || !attempt.taskId) continue;
    const current = latestAttemptByTurn.get(attempt.turnId);
    const createdAt = Number(attempt.createdAt || 0);
    if (!current || createdAt >= current.createdAt) {
      latestAttemptByTurn.set(attempt.turnId, {
        taskId: attempt.taskId,
        createdAt,
      });
    }
  }
  /* Conversation-level livePhase belongs to the newest durable active Turn.
   * Transient lifecycle Turns (send preparation, virtual-user warm-up, image
   * work) already carry their own live-status block. Letting a later transient
   * row win this selection strips the phase from the real active assistant and
   * briefly renders that Turn as a header-only ghost while a new message is
   * being prepared. */
  const mainLiveTurnId = [...(state.laneOrder.main ?? [])].reverse()
    .find((turnId) => {
      const turn = state.turnsById[turnId];
      if (!turn || (turn.status !== 'pending' && turn.status !== 'running')) {
        return false;
      }
      const attempt = turn.currentAttemptId
        ? state.attemptsById[turn.currentAttemptId] : undefined;
      return !transientTurnPresentation(turn) && !attempt?.queueBinding;
    }) ?? null;
  const laneOwners = new Map<string, LaneOwner>();
  let descriptorOrder = 0;
  for (const turn of Object.values(state.turnsById)) {
    if (!turn) continue;
    for (const descriptor of turn.projection._branchLanes ?? []) {
      if (!descriptor.laneId || laneOwners.has(descriptor.laneId)) continue;
      laneOwners.set(descriptor.laneId, {
        parentTurnId: turn.turnId,
        descriptor,
        order: descriptorOrder++,
      });
    }
  }
  for (const [laneId, turnIds] of Object.entries(state.laneOrder)) {
    if (laneId === 'main' || laneOwners.has(laneId)) continue;
    const first = (turnIds ?? []).map((turnId) => state.turnsById[turnId])
      .find((turn): turn is TurnRecord => Boolean(turn));
    if (first?.parentTurnId) {
      laneOwners.set(laneId, {
        parentTurnId: first.parentTurnId,
        order: descriptorOrder++,
      });
    }
  }
  const childrenByTurn = new Map<string, string[]>();
  for (const [laneId, owner] of laneOwners) {
    const lanes = childrenByTurn.get(owner.parentTurnId) ?? [];
    lanes.push(laneId);
    childrenByTurn.set(owner.parentTurnId, lanes);
  }
  for (const lanes of childrenByTurn.values()) {
    lanes.sort((left, right) => (
      (laneOwners.get(left)?.order ?? 0) - (laneOwners.get(right)?.order ?? 0)
      || left.localeCompare(right)
    ));
  }

  const selectedLanes = new Set<string>();
  const selectLane = (
    laneId: string,
    stack: ReadonlySet<string>,
  ): ConversationLaneViewModel => {
    if (stack.has(laneId)) {
      diagnostics.onLaneCycle?.(laneId);
      return {
        laneId,
        parentTurnId: laneOwners.get(laneId)?.parentTurnId ?? null,
        title: laneOwners.get(laneId)?.descriptor?.title ?? 'Branch',
        kind: 'cycle-rejected',
        expanded: false,
        live: false,
        humanTurnCount: 0,
        turns: [],
      };
    }
    selectedLanes.add(laneId);
    const nextStack = new Set(stack);
    nextStack.add(laneId);
    const owner = laneOwners.get(laneId);
    const descriptor = owner?.descriptor;
    const turns = (state.laneOrder[laneId] ?? []).flatMap(
      (turnId): ConversationTurnViewModel[] => {
        const sourceTurn = state.turnsById[turnId];
        if (!sourceTurn) return [];
        const turn = backgroundCommands.project(sourceTurn);
        if (queuedOutputTurnIds.has(turn.turnId)) return [];
        const branches = (childrenByTurn.get(turn.turnId) ?? [])
          .map((childLaneId) => selectLane(childLaneId, nextStack));
        const role = roleFor(turn);
        const taskId = latestAttemptByTurn.get(turn.turnId)?.taskId;
        const taskTimePresentations = turn.currentAttemptId
          ? presentation.taskTimePresentationsByAttempt?.get(turn.currentAttemptId) : undefined;
        const commandPending = state.commandPending[turn.turnId] ?? null;
        const preferredTranslationMode = presentation.translationModeByTurn
          ?.get(turn.turnId) ?? (turn.actor === 'human' ? 'original' : 'translated');
        const translationActivity = presentation.translationActivityByTurn
          ?.get(turn.turnId);
        const laneIdUsed = turn.laneId || laneId;
        const taskIdValue = taskId || '';
        const commandPendingValue = commandPending || '';
        const costSignatureForMetadata = turn.projection.cost
          ? undefined
          : presentation.costSignatureByTurnId?.get(turn.turnId);
        const inspectorEnabled = Boolean(presentation.requestInspectorEnabled);
        const plain = branches.length === 0
          && (autopilotNoticesByTurn.get(turn.turnId)?.length ?? 0) === 0
          && !nativeQueueByInputTurn.has(turn.turnId)
          && !(presentation.artifactsByTurn?.get(turn.turnId)?.length)
          && turn.turnId !== mainLiveTurnId;
        if (plain) {
          const cached = turnVmMemo.get(turn);
          if (cached
              && cached.laneId === laneIdUsed
              && cached.taskId === taskIdValue
              && cached.commandPending === commandPendingValue
              && cached.translationMode === preferredTranslationMode
              && cached.costSignature === (costSignatureForMetadata || '')
              && cached.taskTimePresentations === taskTimePresentations
              && cached.inspector === inspectorEnabled
              && cached.translationActivity === translationActivity) {
            return [cached.vm];
          }
        }
        const finish = presentTurnFinish(turn);
        const leafBlocks = selectTurnBlocks(
          turn, diagnostics, preferredTranslationMode, translationActivity,
        );
        const autopilotNotices = autopilotNoticesByTurn.get(turn.turnId) ?? [];
        const queueItem = nativeQueueByInputTurn.get(turn.turnId);
        const artifacts = presentation.artifactsByTurn?.get(turn.turnId);
        /* Keep the memoized leaf array as the Turn's `blocks` identity when no
         * additive block applies, so an untouched Turn renders with a stable
         * array reference the keyed Surface can skip reconciling. */
        const hasAdditiveBlocks = autopilotNotices.length > 0
          || Boolean(queueItem) || Boolean(artifacts?.length)
          || turn.turnId === mainLiveTurnId;
        const blocks: ConversationBlockViewModel[] = hasAdditiveBlocks
          ? [...leafBlocks]
          : leafBlocks as ConversationBlockViewModel[];
        blocks.push(...autopilotNotices);
        if (queueItem) {
          blocks.push({
            blockId: `queue:${queueItem.queueId}`,
            kind: 'queue-status',
            identitySource: 'contract',
            source: queueItem,
            value: {
              queueId: queueItem.queueId,
              position: Number(queueItem.position || 0),
              text: queueItem.text || '',
              ...(queueItem.steerFallback ? { steerFallback: true } : {}),
              ...(queueItem.backgroundCommand ? { backgroundCommand: queueItem.backgroundCommand } : {}),
            },
          });
        }
        if (artifacts?.length) {
          blocks.push({
            blockId: `${turn.turnId}:artifacts`,
            kind: 'artifacts',
            identitySource: 'contract',
            source: artifacts,
            artifacts,
          });
        }
        if (turn.turnId === mainLiveTurnId
            && !blocks.some((block) => block.kind === 'live-status')) {
          const currentAttempt = turn.currentAttemptId
            ? state.attemptsById[turn.currentAttemptId] : undefined;
          const phaseSource = state.livePhase && typeof state.livePhase === 'object'
            ? state.livePhase as object : currentAttempt ?? turn;
          const liveStatus = liveAttemptPresentation(
            phaseSource,
            turn.status,
          );
          /* A withheld-push wedge means no newer frame can arrive, so any
           * livePhase on record is stale. Present the honest storage-wedge
           * status instead of the misleading generic waiting placeholder
           * (2026-08-26: a 4-minute storage wedge read as "submitted to the
           * agent worker…" while the task was actually retrying writes). */
          if (state.pushWithheld) {
            liveStatus.phase = 'storage_wedged';
            liveStatus.label = '';
            liveStatus.detail = '';
            delete liveStatus.detailKey;
            delete liveStatus.detailArgs;
            delete liveStatus.tools;
            delete liveStatus.toolContext;
            delete liveStatus.toolContextTools;
          }
          /* The bare default 'waiting' placeholder means "no phase frame
           * has arrived". While a thinking or answer block is still
           * streaming at the tail, that block IS the live status — the
           * placeholder beneath it only restates a stall that is not
           * happening. Real phases (waiting_model / retrying / tool_exec /…)
           * carry detail/tools/model and never match; the wedge override
           * above must always render. */
          if (!waitingPlaceholderRedundant(liveStatus, blocks)) {
            blocks.push({
              blockId: 'live-status',
              kind: 'live-status',
              identitySource: 'contract',
              source: phaseSource,
              value: liveStatus,
            });
          }
        }
        const metadata = metadataFrom(
          turn.projection, blocks, preferredTranslationMode,
          translationActivity,
          /* An authoritative projection cost is covered by the
           * projectionRevision footer compare; only the client-side batch
           * fill (historical turns without a stamped cost) needs the
           * presentation-state signature. */
          costSignatureForMetadata,
        );
        if (taskTimePresentations?.length) metadata.taskTimePresentations = taskTimePresentations;
        const turnVm: ConversationTurnViewModel = {
          turnId: turn.turnId,
          presentationId: turn.presentationId || turn.turnId,
          laneId: laneIdUsed,
          parentTurnId: turn.parentTurnId ?? null,
          ordinal: Number(turn.ordinal || 0),
          actor: turn.actor,
          role,
          kind: turn.kind,
          status: turn.status,
          attemptId: turn.currentAttemptId ?? null,
          ...(taskId ? { taskId } : {}),
          projectionRevision: Number(turn.projectionRevision || 0),
          commandPending,
          finish,
          actions: actionsFor(
            turn.actor, laneIdUsed, commandPending, finish,
            metadata.translation, turn.status, taskId,
            inspectorEnabled,
            Boolean(turn.projection.backgroundCommand),
          ),
          blocks,
          branches,
          metadata,
          source: turn,
        };
        if (plain) {
          turnVmMemo.set(turn, {
            laneId: laneIdUsed,
            taskId: taskIdValue,
            commandPending: commandPendingValue,
            translationMode: preferredTranslationMode,
            costSignature: costSignatureForMetadata || '',
            taskTimePresentations,
            inspector: inspectorEnabled,
            translationActivity,
            vm: turnVm,
          });
        }
        return [turnVm];
      },
    );
    return {
      laneId,
      parentTurnId: owner?.parentTurnId ?? null,
      title: laneId === 'main' ? 'Conversation' : descriptor?.title ?? 'Branch',
      ...(descriptor?.label ? { label: descriptor.label } : {}),
      ...(descriptor?.icon ? { icon: descriptor.icon } : {}),
      kind: laneId === 'main' ? 'main' : descriptor?.kind ?? 'branch',
      ...(descriptor?.anchorText ? { anchorText: descriptor.anchorText } : {}),
      ...(descriptor?.parentSelection
        ? { parentSelection: descriptor.parentSelection } : {}),
      expanded: laneId === 'main'
        || presentation.expandedBranchLaneId === laneId,
      live: turns.some((turn) => turn.status === 'pending' || turn.status === 'running'),
      humanTurnCount: turns.filter((turn) => turn.actor === 'human').length,
      ...(state.historyByLane?.[laneId]
        ? { history: { ...state.historyByLane[laneId] } } : {}),
      turns,
    };
  };

  const mainLane = selectLane('main', new Set());
  const orphanLanes = Object.keys(state.laneOrder)
    .filter((laneId) => laneId !== 'main' && !selectedLanes.has(laneId))
    .sort()
    .map((laneId) => selectLane(laneId, new Set()));
  const queue = state.queueItems.filter(
    (item) => item.kind !== 'autopilot' && !item.inputTurnId,
  )
    .sort((left, right) => Number(left.position || 0) - Number(right.position || 0))
    .map((item) => ({
      queueId: item.queueId,
      position: item.position,
      kind: item.kind,
      text: item.text,
      source: item,
    }));
  const decisionLaneId = presentation.expandedBranchLaneId || 'main';
  const lanesToVisit = [mainLane, ...orphanLanes];
  let decisionLane: ConversationLaneViewModel | undefined;
  while (lanesToVisit.length > 0 && !decisionLane) {
    const candidate = lanesToVisit.shift();
    if (!candidate) continue;
    if (candidate.laneId === decisionLaneId) {
      decisionLane = candidate;
      break;
    }
    for (const turn of candidate.turns) lanesToVisit.push(...turn.branches);
  }
  const tail = decisionLane?.turns.at(-1);
  const proposedPlan = tail?.source.projection.proposedPlan;
  const laneCanDecide = decisionLane?.laneId === 'main'
    ? queue.length === 0 : Boolean(decisionLane);
  const planDecision = tail && proposedPlan && laneCanDecide
      && !decisionLane?.live
      && tail.status === 'completed'
      && (tail.actor === 'assistant' || tail.actor === 'planner')
    ? {
      sourceTurnId: tail.turnId,
      sourceProjectionRevision: tail.projectionRevision,
      planId: proposedPlan.planId,
      pending: Boolean(tail.commandPending),
    } : null;
  return {
    conversationId: state.conversationId,
    conversationRevision: state.conversationRevision,
    transport: state.transport,
    mainLane,
    orphanLanes,
    queue,
    planDecision,
    transportNotice: selectTransportNotice(presentation.syncHealth),
  };
}

/* Transport degradation is a conversation-level fact, not a turn failure:
 * while the sync channel is recovering/degraded/offline the transcript may be
 * stale, and the inline banner says exactly that instead of letting a frozen
 * stream read as a model failure (RENDER_CONTRACT: transport state is never
 * presented as turn error). */
function selectTransportNotice(
  syncHealth: ConversationPresentationState['syncHealth'],
): ConversationViewModel['transportNotice'] {
  const healthState = syncHealth?.state;
  if (healthState === 'recovering'
      || healthState === 'degraded'
      || healthState === 'offline') {
    return healthState;
  }
  return null;
}
