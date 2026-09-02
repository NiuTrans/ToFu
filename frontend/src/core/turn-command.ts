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
   * the next tool-call boundary, 'queue' (default) holds the message for
   * dispatch as a fresh turn once the lane settles. A no-op when idle.
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
    injectMode: input.injectMode === 'steer' ? 'steer' : 'queue',
    requestOptions,
  };
}
