export interface TurnCommandConfig {
  flowDefinition?: unknown;
  flowBuiltin?: unknown;
  flowId?: unknown;
  autopilotRunId?: unknown;
  flowRunId?: unknown;
}

export interface TurnSubmissionInput {
  commandId?: unknown;
  settings?: unknown;
  config?: TurnCommandConfig | null;
  signal: AbortSignal;
  /**
   * Per-send delivery decision from the post-send dialog when a turn is
   * already running on the lane: 'steer' injects into the running reply at
   * the next tool-call boundary, while 'queue' holds the message for dispatch
   * as a fresh turn once the lane settles. Omitted when the lane was idle so
   * a server-side lane-busy race is surfaced for an explicit decision.
   */
  injectMode?: 'steer' | 'queue';
}

export interface TurnSubmissionExtra {
  commandId: string;
  settings: unknown;
  laneId: 'main';
  actor: 'planner' | 'assistant';
  kind: 'flow_node' | 'reply';
  runId: string;
  injectMode?: 'steer' | 'queue';
  requestOptions: {
    signal: AbortSignal;
    headers?: { 'Idempotency-Key': string };
  };
}

export interface TurnOperationInput {
  conversationId?: unknown;
  projectionRevision?: unknown;
}

export interface TurnOperationOptions {
  commandId?: unknown;
  inputUpdate?: unknown;
  expectedInputProjectionRevision?: unknown;
  /** Late ask_human answer carried by the ``answer_guidance`` operation. */
  humanResponse?: unknown;
}


function turnProjectionRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

/**
 * Build the input projection for an explicit regenerate action.
 *
 * Existing Turn context remains immutable historical evidence while the user
 * only changes composer/project controls. Regenerate is the explicit boundary
 * that creates a new attempt, so its atomic inputUpdate replaces that evidence
 * with the context live at the click. The legacy `_ctx` field is removed to
 * keep `contextSnapshot` as the single wire representation.
 */
export function rebindTurnInputContext(
  inputProjection: unknown,
  currentSnapshot: unknown,
): Record<string, unknown> {
  const projection = { ...turnProjectionRecord(inputProjection) };
  delete projection._ctx;
  if (currentSnapshot && typeof currentSnapshot === 'object'
      && !Array.isArray(currentSnapshot)) {
    projection.contextSnapshot = {
      blockId: 'turn-context',
      snapshot: { ...currentSnapshot as Record<string, unknown> },
    };
  } else {
    delete projection.contextSnapshot;
  }
  return projection;
}

export function createTurnCommandId(): string {
  try {
    const id = globalThis.crypto?.randomUUID?.();
    if (id) return id;
  } catch {
    // Older embedded webviews can expose crypto without randomUUID access.
  }
  return `cmd_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

export function buildTurnSubmitRequest(
  inputTurn: unknown,
  config: unknown,
  extra?: Record<string, unknown> | null,
): Record<string, unknown> {
  return {
    commandId: createTurnCommandId(),
    inputTurn,
    config,
    ...(extra || {}),
  };
}

export function buildTurnOperationRequest(
  turn: TurnOperationInput,
  operation: string,
  config?: unknown,
  options?: TurnOperationOptions | null,
): Record<string, unknown> {
  const request: Record<string, unknown> = {
    commandId: String(options?.commandId || createTurnCommandId()),
    operation,
    expectedProjectionRevision: Number(turn.projectionRevision || 0),
    config: config || {},
  };
  if (options?.inputUpdate) {
    request.inputUpdate = options.inputUpdate;
    request.expectedInputProjectionRevision = options.expectedInputProjectionRevision;
  }
  if (options?.humanResponse != null) {
    request.humanResponse = String(options.humanResponse);
  }
  return request;
}

/** Build the exact Turn/Attempt command envelope used by the send pipeline. */
export function buildTurnSubmissionExtra(
  input: TurnSubmissionInput,
): TurnSubmissionExtra {
  const config = input.config || {};
  const commandId = String(input.commandId || '');
  const flow = Boolean(
    config.flowDefinition || config.flowBuiltin || config.flowId,
  );
  const requestOptions: TurnSubmissionExtra['requestOptions'] = {
    signal: input.signal,
  };
  if (commandId) {
    requestOptions.headers = { 'Idempotency-Key': commandId };
  }
  return {
    commandId,
    settings: input.settings,
    laneId: 'main',
    actor: 'assistant',
    kind: flow ? 'flow_node' : 'reply',
    runId: String(config.autopilotRunId || config.flowRunId || ''),
    ...(input.injectMode ? { injectMode: input.injectMode } : {}),
    requestOptions,
  };
}
