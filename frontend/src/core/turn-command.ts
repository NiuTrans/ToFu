export interface TurnCommandConfig {
  endpointMode?: boolean;
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
}

export interface TurnSubmissionExtra {
  commandId: string;
  settings: unknown;
  laneId: 'main';
  actor: 'planner' | 'assistant';
  kind: 'endpoint_planner' | 'flow_node' | 'reply';
  runId: string;
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
  const endpoint = Boolean(config.endpointMode);
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
    actor: endpoint ? 'planner' : 'assistant',
    kind: endpoint ? 'endpoint_planner' : flow ? 'flow_node' : 'reply',
    runId: String(config.autopilotRunId || config.flowRunId || ''),
    requestOptions,
  };
}
