/**
 * Program execution evidence from authoritative projected rounds.
 * Entry: programExecutionPresentation. No DOM, state, transport or estimates.
 * Byte counts describe private child delivery and the returned value; they do
 * not represent model token/cost savings or a measured direct-call baseline.
 */
type RecordValue = Readonly<Record<string, unknown>>;

function record(value: unknown): RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : {};
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value : null;
}

/** Human-readable binary quantity; exact byte counters remain in technical details. */
export function programByteSize(bytes: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes; let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit ? Number(value.toFixed(1)) : value} ${units[unit]}`;
}

export function programExecutionPresentation(round: unknown): {
  needsModel: boolean;
  reason: string;
  toolFailureCode: string;
  privateBytes: number | null;
  resultBytes: number | null;
  truncatedResults: number | null;
  callStages: number | null;
  parallelStages: number | null;
  resultReads: number | null;
  toolCallAttempts: number | null;
  successfulToolCalls: number | null;
} {
  const row = record(round);
  const result = record(row.programResult);
  const stats = record(row.programStats);
  const delivery = record(stats.result_delivery);
  const toolCallAttempts = count(stats.tool_calls);
  const successfulToolCalls = count(stats.successful_tool_calls);
  // A running or failed program must not inherit an old observation headline.
  const needsModel = row.programStatus === 'completed'
    && stats.outcome === 'needs_model'
    && result.kind === 'observation'
    && typeof result.reason === 'string' && Boolean(result.reason.trim());
  return {
    needsModel,
    reason: needsModel ? String(result.reason) : '',
    toolFailureCode: needsModel && result.reason === 'tool_not_done'
      ? String(record(record(result.data).error).code ?? '') : '',
    privateBytes: count(delivery.outputBytes),
    resultBytes: count(stats.result_bytes),
    truncatedResults: count(delivery.truncatedResults),
    callStages: count(stats.call_stages),
    parallelStages: count(stats.parallel_stages),
    resultReads: count(stats.result_reads),
    toolCallAttempts,
    successfulToolCalls: toolCallAttempts !== null && successfulToolCalls !== null
      && successfulToolCalls <= toolCallAttempts ? successfulToolCalls : null,
  };
}
