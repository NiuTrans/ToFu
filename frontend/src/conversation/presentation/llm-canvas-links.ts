/** Historical call-local canvas evidence. Policy is shared with Python display.
 * Entry: recoverLlmCanvasLinks(snapshot round). No HTML, network, cache or writes.
 * Metadata stays primary; only missing/empty metadata permits this fallback.
 */
import policy from '../../../../contracts/llm_canvas_links_v1.json';

export interface LlmCanvasLink {
  readonly label: string;
  readonly href: string;
  readonly role: 'input' | 'source' | 'resolved' | 'new';
}
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : undefined;
const own = (value: RecordValue | undefined, key: string): unknown =>
  value && Object.hasOwn(value, key) ? value[key] : undefined;
function parsedRecord(value: unknown): RecordValue | undefined {
  if (typeof value === 'string') {
    if (value.length > policy.limits.serializedChars) return undefined;
    try { return record(JSON.parse(value)); } catch { return undefined; }
  }
  return record(value);
}
function decimal(value: unknown): string {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? String(value) : '';
  return typeof value === 'string' && /^[0-9]+$/.test(value) && /[1-9]/.test(value) ? value : '';
}
/** Exact IDs only. Never parse a title, error message, task ID or arbitrary URL. */
export function llmCanvasExperimentId(value: unknown): string {
  if (typeof value === 'number') return decimal(value);
  if (typeof value !== 'string' || value.length > policy.limits.referenceChars) return '';
  const text = value.trim();
  const id = decimal(text);
  if (id) return id;
  if (/[\s\\<>"'\u0000-\u001f\u007f]/.test(text) || !text.startsWith(policy.origin + '/')) return '';
  try {
    const url = new URL(text);
    if (url.origin !== policy.origin || url.username || url.password || url.hash) return '';
    // Inspect raw paths too: URL normalization must not bless dot-segment URLs.
    const raw = text.slice(policy.origin.length);
    if (url.search) return '';
    return decimal(new RegExp(policy.canvasPathPattern).exec(raw)?.[1]);
  } catch { return ''; }
}
function label(value: unknown, fallback: string): string {
  const text = typeof value === 'string' ? value.trim()
    : typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : fallback;
  const usable = text || fallback;
  return usable.length > policy.limits.labelChars ? usable.slice(0, policy.limits.labelChars - 1) + '…' : usable;
}
function completeRetainedEvidence(value: unknown, retainedSource = true): boolean {
  const evidence = record(value);
  const required = policy.singleItemWrapper.retainedSourceEvidence;
  return !!evidence && Object.entries(required).every(([key, expected]) =>
    (!retainedSource && key === 'projectionKind') || own(evidence, key) === expected)
    // Upstream coverage is independent; unknown is honest for MCP results.
    && ['unknown', 'complete'].includes(String(own(evidence, 'sourceCoverage')))
    && (!retainedSource || (typeof own(evidence, 'artifactRef') === 'string'
      && /^tool-result:[a-f0-9]{64}$/.test(String(own(evidence, 'artifactRef')))
      && typeof own(evidence, 'evidenceId') === 'string' && !!own(evidence, 'evidenceId')
      && typeof own(evidence, 'resultId') === 'string' && !!own(evidence, 'resultId')))
    && !own(evidence, 'error') && own(evidence, 'isError') !== true
    && own(evidence, 'ok') !== false && own(evidence, 'success') !== false
    && own(evidence, 'dry_run') !== true;
}
function successfulResult(value: unknown, evidence: unknown): RecordValue | undefined {
  let envelope = parsedRecord(value);
  if (envelope && Object.hasOwn(envelope, 'items')) {
    // Only the owning projection's exact sparse wrapper is admitted.
    const wrapper = policy.singleItemWrapper;
    const items = own(envelope, 'items');
    if (Object.keys(envelope).length !== wrapper.keys.length
        || !Object.keys(envelope).every(key => wrapper.keys.includes(key))
        || !(own(envelope, 'summary') === wrapper.summary
          || (own(envelope, 'summary') === wrapper.retainedSourceSummary && completeRetainedEvidence(evidence)))
        || !Array.isArray(items) || items.length !== wrapper.itemCount) return undefined;
    envelope = record(items[0]); // No recursive or multi-item harvesting.
  }
  if (!envelope || own(envelope, 'ok') === false || own(envelope, 'truncated') === true
      || ['error', 'failed', 'cancelled', 'canceled', 'partial'].includes(String(own(envelope, 'status')))
      || own(envelope, 'isError') === true || own(envelope, 'error')
      || own(envelope, 'success') === false || own(envelope, 'dry_run') === true) return undefined;
  // An explicit success envelope is required: mere IDs in diagnostics are not evidence.
  const data = own(envelope, 'ok') === true ? parsedRecord(own(envelope, 'data'))
    : own(envelope, 'success') === true ? envelope : undefined;
  if (!data || own(data, 'error') || own(data, 'ok') === false
      || own(data, 'isError') === true || own(data, 'truncated') === true
      || ['error', 'failed', 'cancelled', 'canceled', 'partial'].includes(String(own(data, 'status')))
      || own(data, 'success') === false || own(data, 'dry_run') === true) return undefined;
  return data;
}
/** The existing snapshot reference supplies a bounded full-output witness only
 * when its preview contains every declared character of toolContent. No fetch,
 * private-reference parsing, or prefix/truncated-result harvesting is allowed. */
function snapshotResult(row: RecordValue): unknown {
  if (Object.hasOwn(row, 'toolContent')) return row.toolContent;
  if (Object.hasOwn(row, 'result')) return row.result;
  const reference = record(own(row, '_snapshotToolResult'));
  const preview = own(reference, 'preview');
  const chars = own(reference, 'chars');
  const fields = own(reference, 'fields');
  if (typeof preview !== 'string' || preview.length > policy.limits.serializedChars
      || typeof chars !== 'number' || !Number.isSafeInteger(chars) || chars < 0
      || !Array.isArray(fields) || !fields.includes('toolContent')
      || Array.from(preview).length !== chars) return undefined;
  return preview;
}
export function recoverLlmCanvasLinks(value: unknown): readonly LlmCanvasLink[] {
  const row = record(value);
  if (!row) return [];
  const metadata = record(own(row, '_mcpLinks'));
  if (metadata) for (const key in metadata) if (Object.hasOwn(metadata, key)) return [];
  let name = own(row, 'toolName');
  let args = parsedRecord(own(row, 'toolArgs'));
  if (typeof name === 'string' && policy.progressiveTools.includes(name)) {
    name = own(args, 'name');
    args = parsedRecord(own(args, 'arguments'));
  }
  if (typeof name !== 'string' || !args) return [];
  const prefix = 'mcp__' + policy.server + '__';
  if (!name.startsWith(prefix)) return [];
  const tool = name.slice(prefix.length);
  if (!tool.startsWith(policy.toolPrefix) || !/^[a-z0-9_]+$/.test(tool)) return [];
  const fork = policy.forkTools.includes(tool);
  const links: LlmCanvasLink[] = [];
  const add = (id: string, text: string, role: LlmCanvasLink['role']) => {
    if (!id) return;
    const href = policy.origin + policy.canvasPathPrefix + id;
    if (!links.some(link => link.href === href && link.role === role)) links.push({ label: text, href, role });
  };
  const key = policy.inputKeys.find(key => own(args, key) != null);
  const input = key ? own(args, key) : undefined;
  const values = key === 'ids' ? (Array.isArray(input) ? input.slice(0, policy.limits.inputLinks)
    : typeof input === 'string' ? input.split(',', policy.limits.inputLinks) : []) : key ? [input] : [];
  for (const input of values) {
    const id = llmCanvasExperimentId(input);
    if (id) add(id, (fork ? 'source: ' : '') + id, fork ? 'source' : 'input');
  }
  if (['error', 'failed', 'cancelled', 'canceled', 'partial'].includes(String(own(row, 'status')))
      || own(args, 'dry_run') === true) return links;
  // toolContent is authoritative when present. Do not bypass a failed/malformed
  // envelope by harvesting another result field from the same snapshot.
  const raw = snapshotResult(row);
  const evidence = own(row, 'toolResultEvidence');
  // Never accept contradictory sidecars, even alongside a plain success item.
  if (evidence !== undefined && !completeRetainedEvidence(evidence, false)) return links;
  const result = successfulResult(raw, evidence);
  const id = llmCanvasExperimentId(own(result, policy.resultIdKey));
  if (!result || !id) return links;
  for (const key of policy.resultUrlKeys) {
    const url = own(result, key);
    if (url !== undefined && llmCanvasExperimentId(url) !== id) return links;
  }
  if (fork) add(id, 'new: ' + label(own(result, 'experimentName') ?? own(args, 'name'), id), 'new');
  else if (!links.some(link => link.href === policy.origin + policy.canvasPathPrefix + id)) {
    add(id, label(values.length === 1 ? values[0] : own(result, 'experimentName'), id), 'resolved');
  }
  return links;
}
