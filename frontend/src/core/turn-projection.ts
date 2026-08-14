type JsonRecord = Record<string, unknown>;

export interface ProjectionTurn extends JsonRecord {
  turnId: string;
  actor?: string;
  kind?: string;
  laneId?: string;
  parentTurnId?: string | null;
  status?: string;
  currentAttemptId?: string | null;
  projectionRevision?: number;
  projection?: JsonRecord;
  settlement?: JsonRecord;
  createdAt?: number | string;
}

export interface TurnProjectionState {
  conversationRevision?: number;
  transport?: string;
  turnsById: Record<string, ProjectionTurn | undefined>;
  laneOrder: Record<string, string[] | undefined>;
  commandPending: Record<string, string | null | undefined>;
}

export interface LegacyTurnMessage extends JsonRecord {
  role?: string;
  _msgId?: string;
  _turnId?: string;
  _branchLanes?: BranchDescriptor[];
  branches?: LegacyBranch[];
}

export interface BranchDescriptor extends JsonRecord {
  laneId: string;
  title?: string;
  icon?: string;
  kind?: string;
  anchorText?: string;
  parentSelection?: string;
}

export interface LegacyBranch extends JsonRecord {
  id?: string;
  laneId?: string;
  _laneId?: string;
  parentTurnId?: string | null;
  messages?: LegacyTurnMessage[];
}

export interface TurnProjectionInput {
  state: TurnProjectionState;
  previousMessages?: LegacyTurnMessage[];
  previousLaneMeta?: Record<string, LegacyBranch>;
  now?: () => number;
}

export interface TurnProjectionResult {
  fingerprint: string;
  messages: LegacyTurnMessage[];
  laneMeta: Record<string, LegacyBranch>;
  activeAttemptId: string | null;
  activeBranchAttemptIds: string[];
}

export interface LegacyTurnConversation extends JsonRecord {
  id?: string;
  messages?: LegacyTurnMessage[];
  _v2LaneMeta?: Record<string, LegacyBranch>;
  _v2ProjectionFingerprint?: string;
  _v2Transport?: string;
  _serverRev?: number;
  _finishingStream?: boolean;
  _finishingAttemptId?: string | null;
}

export interface ApplyTurnProjectionInput {
  conversation: LegacyTurnConversation;
  state: TurnProjectionState;
  active?: boolean;
  persist?(conversation: LegacyTurnConversation): void;
  replaceAll?(conversation: LegacyTurnConversation): void;
  buildNavigation?(conversation: LegacyTurnConversation): void;
  renderConversationList?(): void;
  updateSendButton?(): void;
}

function messageRole(actor: string | undefined): string {
  return actor === 'human' || actor === 'critic' || actor === 'virtual_user'
    ? 'user'
    : 'assistant';
}

export function turnToLegacyMessage(
  turn: ProjectionTurn,
  commandPending: string | null | undefined,
  now: () => number = Date.now,
): LegacyTurnMessage {
  const projection = { ...(turn.projection ?? {}) };
  delete projection.role;
  return {
    ...projection,
    role: messageRole(turn.actor),
    _turnId: turn.turnId,
    _attemptId: turn.currentAttemptId ?? null,
    _turnActor: turn.actor,
    _turnKind: turn.kind,
    _turnLaneId: turn.laneId || 'main',
    _turnStatus: turn.status,
    _turnSettlement: turn.settlement ?? {},
    _commandPending: commandPending ?? null,
    _projectionRevision: Number(turn.projectionRevision || 0),
    timestamp: projection.timestamp || turn.createdAt || now(),
  };
}

function turnsInLane(
  state: TurnProjectionState,
  laneId: string,
): ProjectionTurn[] {
  return (state.laneOrder[laneId] ?? [])
    .map((turnId) => state.turnsById[turnId])
    .filter((turn): turn is ProjectionTurn => Boolean(turn));
}

function laneIdOf(branch: LegacyBranch): string {
  return branch._laneId || branch.laneId || branch.id || '';
}

function withoutMessages(branch: LegacyBranch): LegacyBranch {
  const clone = { ...branch };
  delete clone.messages;
  return clone;
}

export function projectionFingerprint(state: TurnProjectionState): string {
  return Object.keys(state.laneOrder).sort().flatMap((laneId) =>
    (state.laneOrder[laneId] ?? []).map((turnId) => {
      const turn = state.turnsById[turnId];
      return `${laneId}:${turn?.turnId}:${turn?.projectionRevision}:${turn?.status}:` +
        `${turn?.currentAttemptId || ''}:${state.commandPending[turnId] || ''}`;
    }),
  ).join('|');
}

/** Pure projection from authoritative turns into the temporary legacy view. */
export function projectTurnState(input: TurnProjectionInput): TurnProjectionResult {
  const { state } = input;
  const now = input.now ?? Date.now;
  const laneMeta: Record<string, LegacyBranch> = Object.create(null) as Record<
    string, LegacyBranch
  >;
  for (const [laneId, metadata] of Object.entries(input.previousLaneMeta ?? {})) {
    laneMeta[laneId] = withoutMessages(metadata);
  }

  for (const oldParent of input.previousMessages ?? []) {
    const parentTurnId = oldParent._turnId || oldParent._msgId;
    for (const branch of oldParent.branches ?? []) {
      const laneId = laneIdOf(branch);
      if (!laneId) continue;
      laneMeta[laneId] = {
        ...(laneMeta[laneId] ?? {}),
        ...withoutMessages(branch),
        parentTurnId: parentTurnId || laneMeta[laneId]?.parentTurnId,
      };
    }
  }

  const messageFor = (turn: ProjectionTurn): LegacyTurnMessage =>
    turnToLegacyMessage(turn, state.commandPending[turn.turnId], now);
  const messages = turnsInLane(state, 'main').map(messageFor);

  for (const parent of messages) {
    const descriptors = Array.isArray(parent._branchLanes)
      ? parent._branchLanes
      : [];
    if (!descriptors.length) continue;
    parent.branches = descriptors.filter((item) => Boolean(item?.laneId)).map((descriptor) => {
      const laneId = descriptor.laneId;
      const previous = laneMeta[laneId] ?? {};
      const branch: LegacyBranch = {
        ...previous,
        id: laneId,
        _laneId: laneId,
        title: descriptor.title || previous.title || 'Branch',
        icon: descriptor.icon || previous.icon || '⑂',
        kind: descriptor.kind || previous.kind || 'branch',
        anchorText: descriptor.anchorText || '',
        parentSelection: descriptor.parentSelection || '',
        messages: turnsInLane(state, laneId).map(messageFor),
      };
      laneMeta[laneId] = {
        ...withoutMessages(branch),
        parentTurnId: parent._turnId,
      };
      return branch;
    });
  }

  for (const [laneId, ids] of Object.entries(state.laneOrder)) {
    if (laneId === 'main' || !ids?.length) continue;
    const laneTurns = turnsInLane(state, laneId);
    const parentTurnId = laneMeta[laneId]?.parentTurnId
      || laneTurns[0]?.parentTurnId
      || null;
    const parent = messages.find((item) => item._turnId === parentTurnId);
    if (!parent) continue;
    const previous = laneMeta[laneId] ?? {};
    const branch: LegacyBranch = {
      ...previous,
      id: previous.id || laneId,
      _laneId: laneId,
      title: previous.title || 'Branch',
      icon: previous.icon || '⑂',
      messages: laneTurns.map(messageFor),
    };
    const branches = parent.branches ?? (parent.branches = []);
    const index = branches.findIndex((item) => laneIdOf(item) === laneId);
    if (index >= 0) branches[index] = branch;
    else branches.push(branch);
    laneMeta[laneId] = { ...withoutMessages(branch), parentTurnId };
  }

  const live = turnsInLane(state, 'main').find(
    (turn) => turn.status === 'pending' || turn.status === 'running',
  );
  const activeBranchAttemptIds: string[] = [];
  for (const laneId of Object.keys(state.laneOrder)) {
    if (laneId === 'main') continue;
    for (const turn of turnsInLane(state, laneId)) {
      if ((turn.status === 'pending' || turn.status === 'running')
          && turn.currentAttemptId) {
        activeBranchAttemptIds.push(turn.currentAttemptId);
      }
    }
  }

  return {
    fingerprint: projectionFingerprint(state),
    messages,
    laneMeta,
    activeAttemptId: live?.currentAttemptId || null,
    activeBranchAttemptIds: [...new Set(activeBranchAttemptIds)],
  };
}

/** Commit the authoritative projection to the temporary conversation view. */
export function applyTurnStateProjection(input: ApplyTurnProjectionInput): boolean {
  const { conversation, state } = input;
  const result = projectTurnState({
    state,
    previousMessages: conversation.messages ?? [],
    previousLaneMeta: conversation._v2LaneMeta ?? {},
  });
  if (conversation._v2ProjectionFingerprint === result.fingerprint) {
    const transportChanged = conversation._v2Transport !== state.transport;
    conversation._v2Transport = state.transport;
    if (transportChanged) {
      // Connection status affects badges/buttons, not the message projection.
      // Repainting unchanged messages here races terminal DOM settlement.
      input.renderConversationList?.();
      input.updateSendButton?.();
    }
    return false;
  }

  conversation._v2ProjectionFingerprint = result.fingerprint;
  conversation._v2Transport = state.transport;
  conversation._turnProtocolV2 = true;
  conversation._serverRev = Number(
    state.conversationRevision || conversation._serverRev || 0,
  );
  conversation._v2LaneMeta = result.laneMeta;
  conversation.messages = result.messages;
  conversation._activeAttemptId = result.activeAttemptId;
  conversation._activeBranchAttemptIds = new Set(result.activeBranchAttemptIds);
  // Internal task ids are never restored into public conversation state.
  conversation.activeTaskId = null;
  conversation._needsLoad = false;

  input.persist?.(conversation);
  if (input.active) {
    input.replaceAll?.(conversation);
    input.buildNavigation?.(conversation);
  }
  input.renderConversationList?.();
  input.updateSendButton?.();
  return true;
}
