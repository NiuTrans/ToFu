/** Safe failure copy for tool-card summaries, never an execution verdict.
 * Entry: toolFailureText. Known historical wrappers are inspected within fixed
 * parse/visit/depth budgets; raw evidence remains with the result disclosure.
 * No DOM, transport, title fallback, or tool reexecution.
 */
export const TOOL_FAILURE_PRESENTATION_LIMITS = Object.freeze({
  parseUnits: 32_768, summaryUnits: 512, depth: 6, visits: 32,
});
type RecordValue = Readonly<Record<string, unknown>>;
const PRIVATE_METADATA = /tool-result:|\btr:|\btrq\d+\.|nextCursor|resumeCursor|artifactRef|artifact_ref|\bcursor\b|\b[a-f0-9]{64}\b/i;

export function toolFailureText(raw: unknown, fallback: string): string {
  let visits = 0;
  const clip = (value: string) => value.length > TOOL_FAILURE_PRESENTATION_LIMITS.summaryUnits
    ? `${value.slice(0, TOOL_FAILURE_PRESENTATION_LIMITS.summaryUnits - 1)}…` : value;
  function inspect(value: unknown, depth: number): string {
    if (depth > TOOL_FAILURE_PRESENTATION_LIMITS.depth
        || ++visits > TOOL_FAILURE_PRESENTATION_LIMITS.visits) return '';
    if (typeof value === 'string') {
      if (value.length > TOOL_FAILURE_PRESENTATION_LIMITS.parseUnits) return '';
      const text = value.trim().replace(/^MCP\s+Error:\s*/i, '');
      if (/^[{[\"]/.test(text)) {
        try { return inspect(JSON.parse(text) as unknown, depth + 1); }
        catch { return ''; }
      }
      // Never show a partial/malformed serialized wrapper as prose. Check all
      // supplied prose before clipping so a private suffix cannot leak a prefix.
      if (/[{[]/.test(text) || PRIVATE_METADATA.test(text)) return '';
      return clip(text.split(/[\r\n]/, 1)[0].trim());
    }
    if (!value || typeof value !== 'object') return '';
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 16)) {
        if (item && typeof item === 'object' && 'status' in item
            && !['error', 'failed'].includes(String(item.status))) continue;
        const result = inspect(item, depth + 1);
        if (result) return result;
      }
      return '';
    }
    const object = value as RecordValue;
    const field = (key: string): unknown => Object.prototype.hasOwnProperty.call(object, key) ? object[key] : undefined;
    // Failure fields outrank summaries: an artifact envelope may have a giant
    // serialized MCP summary but also a short structured error_detail.message.
    for (const key of ['error', 'error_detail', 'items', 'message', 'detail', 'summary', 'result', 'content']) {
      const candidate = field(key);
      if (candidate === undefined) continue;
      const result = inspect(candidate, depth + 1);
      if (!result) continue;
      const code = field('code');
      const safeCode = typeof code === 'string' && /^[a-z][a-z0-9_.-]{0,79}$/i.test(code)
        && !PRIVATE_METADATA.test(code) ? code : '';
      return clip(safeCode ? `${safeCode}: ${result}` : result);
    }
    return '';
  }
  return inspect(raw, 0) || clip(fallback);
}
