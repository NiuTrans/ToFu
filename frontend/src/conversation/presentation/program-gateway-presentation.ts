/** Read-only compatibility projection for exact native execute_program history.
 * Synthetic metadata remains primary; no stats are derived from code/results.
 * Parsing is bounded and call-local. Durable records are never changed.
 */
import { resolveToolResultText } from './tool-result-surfaces';

type Row = Readonly<Record<string, unknown>>;
function record(value: unknown): Row {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Row : {};
}
export function isNativeProgramGateway(value: unknown): boolean {
  return record(value).toolName === 'execute_program';
}
export function normalizeProgramPresentation(value: unknown): Row {
  const row = record(value);
  if (row._programSynthetic || !isNativeProgramGateway(row)) return row;
  let args = row.toolArgs;
  if (typeof args === 'string') {
    if (args.length > 65_536) args = undefined;
    else { try { args = JSON.parse(args) as unknown; } catch { args = undefined; } }
  }
  const source = record(args).code;
  const reference = record(row._snapshotToolResult);
  const authoritative = resolveToolResultText(row);
  const snapshot = typeof reference.preview === 'string' ? reference.preview : undefined;
  const complete = !row._snapshotToolResult || (snapshot !== undefined
    && typeof reference.chars === 'number' && Number.isSafeInteger(reference.chars)
    && reference.chars === Array.from(snapshot).length);
  const raw = row._snapshotToolResult ? snapshot : authoritative?.text;
  let result: unknown = raw;
  if (complete && typeof raw === 'string' && raw.length <= 65_536) {
    try { result = JSON.parse(raw) as unknown; } catch { /* Plain output remains exact. */ }
  }
  return { ...row, programCode: typeof source === 'string' ? source : '',
    programResult: result, programStats: undefined,
    programStatus: row.status === 'searching' ? 'running'
      : row.status === 'error' || row.status === 'failed' ? 'error'
        : row.status === 'done' ? 'returned' : 'incomplete',
    _programGatewayPresentation: true, _programResultComplete: complete,
    _programRawResult: raw };
}

/** Preview excludes technical artifact/cursor envelopes; raw evidence is untouched. */
export function safeProgramPreview(value: string): string {
  return /artifact[_]?ref|\bcursor\b|nextCursor|resumeCursor|tool-result:|\btr:|\btrq\d+\.|\b[a-f0-9]{64}\b/i.test(value) ? '' : value;
}
