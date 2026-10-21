import { entityRecordValues } from '../domain/turn-store';
/**
 * Pure ConversationViewModel composition over immutable TurnStore. Owns lane/attempt
 * joins and Turn memoization; delegates content projection to view-model/.
 * Entry: selectConversationViewModel, createConversationViewModelSelector.
 * Dependencies: background-command-completion, conversation-sync.generated, turn-store, transient-turn, turn-finish, types, turn-blocks, turn-metadata, live-turns.
 */
import type { AutomationManagementPresentation } from '../../api/automation.generated';
import {
  createBackgroundCommandCompletionProjection,
} from './background-command-completion';
import {
  type BranchLaneDescriptor,
  type ConversationQueueItem,
  type TurnRecord,
} from '../../api/conversation-sync.generated';
import {
  type TurnState, type TurnStateUpdate,
} from '../domain/turn-store';
import { canReuseSelection, snapshotPresentation, samePresentation } from './view-model/selection-deltas';
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
  selectGoalProgress,
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
  reasoningAttempt: object | undefined;
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

type BackgroundProjection = ReturnType<typeof createBackgroundCommandCompletionProjection>;

/** What a QUEUING Turn can say about its waiting message. A durable queue row
 * carries the real position and producer markers; the row-less fallback
 * (`position: 0`) knows only that the Turn's bound output Turn waits. */
type ConversationQueueExplanation = {
  queueId: string;
  position: number;
  text: string;
  steerFallback?: boolean;
  backgroundCommand?: ConversationQueueItem['backgroundCommand'];
};

function selectJoins(state: TurnState, presentation: ConversationPresentationState,
  backgroundCommands: BackgroundProjection) {
  backgroundCommands.update(state);
  const autopilotNoticesByTurn = selectAutopilotRunNotices(state, presentation);
  const nativeQueueByInputTurn = new Map<string, ConversationQueueExplanation>(
    state.queueItems.flatMap((item) => (
      item.inputTurnId ? [[item.inputTurnId, item] as const] : []
    )),
  );
  /* The bound attempt is the durable proof that an output Turn waits in the
   * queue; the queue row is a separate read of the same commit. Treating the
   * row as the only evidence left a pending output Turn with neither a
   * live-status nor a queue block — a header-only ghost — whenever that read
   * lagged or came back empty. Both surfaces derive from state we already hold. */
  const queuedBindingByOutputTurn = new Map<string, string>();
  for (const attempt of entityRecordValues(state.attemptsById)) {
    const queueId = attempt?.queueBinding?.queueId;
    if (attempt?.turnId && queueId) {
      queuedBindingByOutputTurn.set(attempt.turnId, queueId);
    }
  }
  const queuedOutputTurnIds = new Set<string>(queuedBindingByOutputTurn.keys());
  for (const item of state.queueItems) {
    if (item.outputTurnId) queuedOutputTurnIds.add(item.outputTurnId);
  }
  /* Row-less explanation for the QUEUING Turn: its own content plus its bound
   * output Turn's identity, never a fabricated queue position. */
  const queuedInputFallback = new Map<string, ConversationQueueExplanation>();
  for (const [outputTurnId, queueId] of queuedBindingByOutputTurn) {
    const inputTurnId = state.turnsById[outputTurnId]?.parentTurnId;
    if (!inputTurnId || nativeQueueByInputTurn.has(inputTurnId)) continue;
    const content = state.turnsById[inputTurnId]?.projection?.content;
    queuedInputFallback.set(inputTurnId, {
      queueId,
      position: 0,
      text: typeof content === 'string' ? content : '',
    });
  }
  const queueExplanationFor = (turnId: string) => (
    nativeQueueByInputTurn.get(turnId) ?? queuedInputFallback.get(turnId)
  );
  const latestAttemptByTurn = new Map<string, {
    taskId: string;
    createdAt: number;
  }>();
  for (const attempt of entityRecordValues(state.attemptsById)) {
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
  const goalProgressByTurn = selectGoalProgress(state, mainLiveTurnId);
  const laneOwners = new Map<string, LaneOwner>();
  let descriptorOrder = 0;
  for (const turn of entityRecordValues(state.turnsById)) {
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

  return { autopilotNoticesByTurn, nativeQueueByInputTurn, queueExplanationFor,
    queuedOutputTurnIds, latestAttemptByTurn, mainLiveTurnId, goalProgressByTurn,
    laneOwners, childrenByTurn };
}

interface CompiledSelection {
  joins: ReturnType<typeof selectJoins>;
  lanes: Map<string, ConversationLaneViewModel>;
  locations: Map<string, { laneId: string; index: number }>;
  viewModel: ConversationViewModel;
  cyclic: boolean;
}

const snapshotBackgroundCommands = createBackgroundCommandCompletionProjection();

/** Snapshot selection remains available to callers without canonical deltas. */
export function selectConversationViewModel(
  state: TurnState, diagnostics: ConversationViewModelDiagnostics = {},
  presentation: ConversationPresentationState = {},
): ConversationViewModel {
  return composeSelection(state, diagnostics, presentation,
    snapshotBackgroundCommands).viewModel;
}

/** One latest compiled view per mounted session; never retains an event log. */
export function createConversationViewModelSelector() {
  let backgroundCommands = createBackgroundCommandCompletionProjection();
  let compiled: CompiledSelection | undefined;
  let observed: TurnState | undefined;
  let display = snapshotPresentation({});
  let diagnosticsUsed: ConversationViewModelDiagnostics | undefined;
  let reusable = false;
  const dirty = new Set<string>();
  return {
    observe(state: TurnState, update?: TurnStateUpdate): void {
      if (state === observed) return;
      if (!compiled || !observed || !update || update.previousState !== observed
          || !canReuseSelection(observed, state, update)) {
        reusable = false;
        dirty.clear();
      } else if (reusable) {
        for (const id of update.changes!.turnIds) dirty.add(id);
        for (const id of update.changes!.attemptIds) {
          const turnId = state.attemptsById[id]?.turnId;
          if (turnId) dirty.add(turnId);
        }
      }
      observed = state;
    },
    select(state: TurnState, diagnostics: ConversationViewModelDiagnostics = {},
      presentation: ConversationPresentationState = {}): ConversationViewModel {
      const usePrevious = reusable && state === observed && compiled && !compiled.cyclic
        && diagnostics.onCompatibilityIdentity === diagnosticsUsed?.onCompatibilityIdentity
        && diagnostics.onLaneCycle === diagnosticsUsed?.onLaneCycle
        && samePresentation(display, presentation);
      compiled = composeSelection(state, diagnostics, presentation, backgroundCommands,
        usePrevious ? compiled : undefined, dirty);
      observed = state;
      display = snapshotPresentation(presentation);
      diagnosticsUsed = diagnostics;
      dirty.clear();
      reusable = true;
      return compiled.viewModel;
    },
    dispose(): void {
      compiled = undefined; observed = undefined; diagnosticsUsed = undefined;
      display = snapshotPresentation({}); dirty.clear(); reusable = false;
      backgroundCommands = createBackgroundCommandCompletionProjection();
    },
  };
}

function composeSelection(
  state: TurnState, diagnostics: ConversationViewModelDiagnostics,
  presentation: ConversationPresentationState, backgroundCommands: BackgroundProjection,
  previous?: CompiledSelection, dirty: ReadonlySet<string> = new Set(),
): CompiledSelection {
  const joins = previous?.joins ?? selectJoins(state, presentation, backgroundCommands);
  const { autopilotNoticesByTurn, queueExplanationFor, queuedOutputTurnIds,
    latestAttemptByTurn, mainLiveTurnId, goalProgressByTurn, laneOwners, childrenByTurn } = joins;
  const lanes = new Map(previous?.lanes);
  const locations = previous?.locations ?? new Map<string, { laneId: string; index: number }>();
  const affected = new Map<string, Set<string>>();
  // A changed descendant also replaces each containing Turn/lane, once.
  for (const changedId of dirty) {
    let id: string | undefined = changedId;
    const visited = new Set<string>();
    while (id && !visited.has(id)) {
      visited.add(id);
      const location = locations.get(id);
      if (!location) break;
      const ids = affected.get(location.laneId) ?? new Set<string>();
      ids.add(id); affected.set(location.laneId, ids);
      id = laneOwners.get(location.laneId)?.parentTurnId;
    }
  }
  let cyclic = false;

  const selectedLanes = new Set<string>();
  const selectLane = (
    laneId: string,
    stack: ReadonlySet<string>,
  ): ConversationLaneViewModel => {
    if (stack.has(laneId)) {
      cyclic = true;
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
    const cachedLane = previous?.lanes.get(laneId);
    if (cachedLane && !affected.has(laneId)) return cachedLane;
    selectedLanes.add(laneId);
    const nextStack = new Set(stack);
    nextStack.add(laneId);
    const owner = laneOwners.get(laneId);
    const descriptor = owner?.descriptor;
    const selectTurn = (turnId: string): ConversationTurnViewModel[] => {
        const sourceTurn = state.turnsById[turnId];
        if (!sourceTurn) return [];
        const turn = backgroundCommands.project(sourceTurn);
        if (queuedOutputTurnIds.has(turn.turnId)) return [];
        const branches = (childrenByTurn.get(turn.turnId) ?? [])
          .map((childLaneId) => selectLane(childLaneId, nextStack));
        const role = roleFor(turn);
        const taskId = latestAttemptByTurn.get(turn.turnId)?.taskId;
        const reasoningAttempt = turn.currentAttemptId
          ? state.attemptsById[turn.currentAttemptId] : undefined;
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
          && !queueExplanationFor(turn.turnId)
          && !(presentation.artifactsByTurn?.get(turn.turnId)?.length)
          && turn.turnId !== mainLiveTurnId && !goalProgressByTurn.has(turn.turnId);
        if (plain) {
          const cached = turnVmMemo.get(turn);
          if (cached
              && cached.laneId === laneIdUsed
              && cached.taskId === taskIdValue
              && cached.commandPending === commandPendingValue
              && cached.translationMode === preferredTranslationMode
              && cached.costSignature === (costSignatureForMetadata || '')
              && cached.taskTimePresentations === taskTimePresentations
              && cached.reasoningAttempt === reasoningAttempt
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
        const queueItem = queueExplanationFor(turn.turnId);
        const artifacts = presentation.artifactsByTurn?.get(turn.turnId);
        /* Keep the memoized leaf array as the Turn's `blocks` identity when no
         * additive block applies, so an untouched Turn renders with a stable
         * array reference the keyed Surface can skip reconciling. */
        const hasAdditiveBlocks = autopilotNotices.length > 0
          || Boolean(queueItem) || Boolean(artifacts?.length)
          || turn.turnId === mainLiveTurnId || goalProgressByTurn.has(turn.turnId);
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
        if ((turn.turnId === mainLiveTurnId || goalProgressByTurn.has(turn.turnId))
            && !blocks.some((block) => block.kind === 'live-status')) {
          const currentAttempt = turn.currentAttemptId
            ? state.attemptsById[turn.currentAttemptId] : undefined;
          const phaseSource = turn.turnId === mainLiveTurnId
              && state.livePhase && typeof state.livePhase === 'object'
            ? state.livePhase as object : currentAttempt ?? turn;
          const liveStatus = liveAttemptPresentation(
            phaseSource,
            turn.status,
          );
          liveStatus.goalProgress = goalProgressByTurn.get(turn.turnId);
          /* A withheld-push wedge means no newer frame can arrive, so any
           * livePhase on record is stale. Present the honest storage-wedge
           * status instead of the misleading generic waiting placeholder
           * (2026-08-26: a 4-minute storage wedge read as "submitted to the
           * agent worker…" while the task was actually retrying writes). */
          if (state.pushWithheld) {
            liveStatus.phase = 'storage_wedged';
            delete liveStatus.goalProgress;
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
          costSignatureForMetadata, turn, reasoningAttempt,
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
            reasoningAttempt,
            inspector: inspectorEnabled,
            translationActivity,
            vm: turnVm,
          });
        }
        return [turnVm];
    };
    const turns = cachedLane ? [...cachedLane.turns]
      : (state.laneOrder[laneId] ?? []).flatMap(selectTurn);
    if (cachedLane) {
      for (const id of affected.get(laneId) ?? []) {
        const location = locations.get(id)!;
        turns[location.index] = selectTurn(id)[0]!;
      }
    } else {
      turns.forEach((turn, index) => locations.set(turn.turnId, { laneId, index }));
    }
    const lane: ConversationLaneViewModel = {
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
      live: cachedLane?.live ?? turns.some((turn) => turn.status === 'pending' || turn.status === 'running'),
      humanTurnCount: cachedLane?.humanTurnCount ?? turns.filter((turn) => turn.actor === 'human').length,
      ...(state.historyByLane?.[laneId]
        ? { history: { ...state.historyByLane[laneId] } } : {}),
      turns,
    };
    lanes.set(laneId, lane);
    return lane;
  };

  const mainLane = selectLane('main', new Set());
  const orphanLanes = (previous ? previous.viewModel.orphanLanes.map((lane) => lane.laneId)
    : Object.keys(state.laneOrder)
      .filter((laneId) => laneId !== 'main' && !selectedLanes.has(laneId)).sort())
    .map((laneId) => selectLane(laneId, new Set()));
  const queue = previous?.viewModel.queue ?? state.queueItems.filter(
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
  const decisionLane = lanes.get(decisionLaneId);
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
  const viewModel: ConversationViewModel = {
    conversationId: state.conversationId,
    conversationRevision: state.conversationRevision,
    transport: state.transport,
    mainLane,
    orphanLanes,
    queue,
    planDecision,
    transportNotice: selectTransportNotice(presentation.syncHealth),
  };
  return { joins, lanes, locations, viewModel, cyclic };
}

/* The sync channel can flap for a few hundred ms during routine reconnects.
 * A notice that appears and vanishes inside that window only distracts; we
 * require the unhealthy state to persist for ~1 s before we surface it, and
 * clear it immediately once the channel is healthy again. */
const TRANSPORT_NOTICE_DEBOUNCE_MS = 1000;
let transportNoticeDebounce: {
  state: 'recovering' | 'degraded' | 'offline';
  shownAt: number;
} | null = null;

function selectTransportNotice(
  syncHealth: ConversationPresentationState['syncHealth'],
): ConversationViewModel['transportNotice'] {
  const healthState = syncHealth?.state;
  if (healthState !== 'recovering'
      && healthState !== 'degraded'
      && healthState !== 'offline') {
    transportNoticeDebounce = null;
    return null;
  }
  const now = Date.now();
  if (transportNoticeDebounce?.state !== healthState) {
    transportNoticeDebounce = { state: healthState, shownAt: now + TRANSPORT_NOTICE_DEBOUNCE_MS };
    return null;
  }
  if (now < transportNoticeDebounce.shownAt) return null;
  return healthState;
}


