/** Bounded lexical activity evidence, never execution, intent, or a call count.
 * Only direct tools.member(...) syntax is recognized. Strings/comments/templates
 * and regexp literals are skipped; dynamic/aliased calls remain unknown.
 */
export function programActivityMembers(source: unknown): readonly string[] {
  if (typeof source !== 'string') return [];
  const code = source.slice(0, 32_768);
  const tokens: string[] = [];
  for (let i = 0; i < code.length;) {
    const c = code[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '/' && code[i + 1] === '/') {
      i = code.indexOf('\n', i + 2); if (i < 0) break; continue;
    }
    if (c === '/' && code[i + 1] === '*') {
      const end = code.indexOf('*/', i + 2); if (end < 0) break;
      i = end + 2; continue;
    }
    if (c === '"' || c === "'" || c === '`' || c === '/') {
      // Conservatively skip slash-delimited text too: division may hide evidence,
      // but a regexp must never manufacture an activity label.
      const quote = c; tokens.push('#'); i++;
      let characterClass = false;
      while (i < code.length) {
        if (code[i] === '\\') { i += 2; continue; }
        if (quote === '/' && code[i] === '[') characterClass = true;
        if (quote === '/' && code[i] === ']') characterClass = false;
        if (code[i++] === quote && !characterClass) break;
      }
      continue;
    }
    const identifier = /^[A-Za-z_$][\w$]*/.exec(code.slice(i));
    if (identifier) { tokens.push(identifier[0]); i += identifier[0].length; }
    else { tokens.push(c); i++; }
  }
  const supported = new Set(['inputs', 'result', 'invoke', 'keep', 'observe']);
  const members = new Set<string>();
  for (let i = 0; i + 3 < tokens.length; i++) {
    if (tokens[i] === 'tools' && tokens[i - 1] !== '.'
        && tokens[i + 1] === '.' && supported.has(tokens[i + 2])
        && tokens[i + 3] === '(') members.add(tokens[i + 2]);
  }
  return [...members];
}

type ProgramOccurrence = Readonly<Record<string, unknown>>;

function gatewayOccurrence(value: unknown): ProgramOccurrence | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const round = value as ProgramOccurrence;
  return (round.toolName === 'execute_program' || round.toolName === 'execute_tools')
    && typeof round.toolCallId === 'string' && round.toolCallId
    && typeof round.llmRound === 'number' && Number.isSafeInteger(round.llmRound)
    ? round : undefined;
}

/** Missing historical scope is compatible, but never resolves multiple owners. */
function programScope(round: ProgramOccurrence): [unknown, unknown] {
  return [(round.attemptId ?? round._attemptId) || null, (round.taskId ?? round._taskId) || null];
}

/** Projection-local index: four scope projections per parent/explicit local alias.
 * Queries visit at most four disjoint buckets, even for reused provider IDs.
 * No durable data or resident cache is created. */
export function createProgramGatewayResolver<T>(context: readonly T[]): (value: unknown) => T | undefined {
  const index = new Map<string, { count: number; value: T }>();
  const key = (id: unknown, round: unknown, mask: number, attempt: unknown, task: unknown): string =>
    JSON.stringify([id, round, mask, ...(mask & 1 ? [attempt] : []), ...(mask & 2 ? [task] : [])]);
  for (const value of context) {
    const parent = value as ProgramOccurrence;
    if (parent?._programSynthetic !== true || typeof parent._programCallId !== 'string'
        || !parent._programCallId || typeof parent.llmRound !== 'number'
        || !Number.isSafeInteger(parent.llmRound)) continue;
    const ids = [parent._programCallId];
    // This is the producer's explicit local alias, never source/intent matching.
    if (parent.programBackend === 'local_javascript') {
      const original = parent._programCallId.replace(/__tofu_local_[1-9]\d*$/, '');
      if (original && original !== parent._programCallId) ids.push(original);
    }
    const [attempt, task] = programScope(parent);
    for (const id of ids) for (let mask = 0; mask < 4; mask++) {
      const bucketKey = key(id, parent.llmRound, mask, attempt, task);
      const bucket = index.get(bucketKey);
      if (bucket) bucket.count += 1;
      else index.set(bucketKey, { count: 1, value });
    }
  }
  return value => {
    if (!index.size) return undefined;
    const gateway = gatewayOccurrence(value);
    if (!gateway) return undefined;
    const [attempt, task] = programScope(gateway);
    const mask = (attempt === null ? 0 : 1) | (task === null ? 0 : 2);
    let count = 0;
    let owner: T | undefined;
    for (const a of attempt === null ? [null] : [attempt, null]) {
      for (const t of task === null ? [null] : [task, null]) {
        const bucket = index.get(key(gateway.toolCallId, gateway.llmRound, mask, a, t));
        if (!bucket) continue;
        count += bucket.count;
        if (count > 1) return undefined;
        owner = bucket.value;
      }
    }
    return owner;
  };
}

/** Single-lookup compatibility port; projection pipelines build the index once. */
export function programGatewayOwner<T>(value: unknown, context: readonly T[]): T | undefined {
  const gateway = gatewayOccurrence(value);
  if (!gateway) return undefined;
  const [attempt, task] = programScope(gateway);
  let owner: T | undefined;
  // Legacy per-row callers must not rebuild a whole batch index per lookup.
  for (const candidate of context) {
    const parent = candidate as ProgramOccurrence;
    if (parent?._programSynthetic !== true || typeof parent._programCallId !== 'string'
        || parent.llmRound !== gateway.llmRound) continue;
    const id = parent._programCallId;
    if (id !== gateway.toolCallId && (parent.programBackend !== 'local_javascript'
        || id.replace(/__tofu_local_[1-9]\d*$/, '') !== gateway.toolCallId)) continue;
    const [parentAttempt, parentTask] = programScope(parent);
    if ((attempt !== null && parentAttempt !== null && attempt !== parentAttempt)
        || (task !== null && parentTask !== null && task !== parentTask)) continue;
    if (owner !== undefined) return undefined;
    owner = candidate;
  }
  return owner;
}

/** Compatibility callers share the same occurrence admission as placement. */
export function programOwnsGateway(value: unknown, context: readonly unknown[]): boolean {
  return programGatewayOwner(value, context) !== undefined;
}

/** Counts from the returned SDK directory shape, never inferred job counts. */
export function retainedInputDirectoryCounts(value: unknown): { returned: number; total: number } | null {
  const row = value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>> : {};
  if (!Array.isArray(row.inputs) || !Object.hasOwn(row, 'nextOffset')
      || !(row.nextOffset === null || typeof row.nextOffset === 'string')
      || typeof row.total !== 'number' || !Number.isSafeInteger(row.total)
      || row.total < row.inputs.length) return null;
  return { returned: row.inputs.length, total: row.total };
}

/** Browser evidence is a bounded excerpt, not a replacement for durable output. */
export function boundedProgramDisplay(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value.length <= 16_384
    ? value : value.slice(0, 16_384) + '\n… [display truncated]';
  let nodes = 0;
  let remaining = 16_384;
  let truncated = false;
  const seen = new WeakSet<object>();
  const visit = (item: unknown, depth: number): unknown => {
    if (++nodes > 512 || remaining <= 0 || depth > 10) {
      truncated = true; return '[display truncated]';
    }
    remaining -= 8;
    if (typeof item === 'string') {
      const text = item.slice(0, Math.max(0, remaining)); remaining -= text.length;
      if (text.length !== item.length) truncated = true;
      return text;
    }
    if (!item || typeof item !== 'object') return item;
    if (seen.has(item)) { truncated = true; return '[circular]'; }
    seen.add(item);
    if (Array.isArray(item)) {
      const output = [];
      for (let i = 0; i < item.length; i++) {
        if (nodes >= 512 || remaining <= 0) { truncated = true; break; }
        output.push(visit(item[i], depth + 1));
      }
      return output;
    }
    const output: Record<string, unknown> = Object.create(null);
    for (const key in item) {
      if (!Object.hasOwn(item, key)) continue;
      if (nodes >= 512 || remaining <= 0) { truncated = true; break; }
      remaining -= key.length;
      output[key.slice(0, 256)] = visit((item as Record<string, unknown>)[key], depth + 1);
    }
    return output;
  };
  try {
    const text = JSON.stringify(visit(value, 0), null, 2) ?? String(value);
    return text.slice(0, 16_384) + (truncated || text.length > 16_384 ? '\n… [display truncated]' : '');
  } catch { return '[result unavailable for display]'; }
}

/** Recognize only the SDK keeper receipt; references stay in raw evidence. */
export function retainedProgramValueBytes(value: unknown): number | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Readonly<Record<string, unknown>>;
  if (Object.keys(row).length !== 4 || row.kind !== 'program_value'
      || typeof row.ref !== 'string' || !/^input_[a-f0-9]{32}$/.test(row.ref)
      || row.sourceCoverage !== 'unknown'
      || typeof row.bytes !== 'number' || !Number.isSafeInteger(row.bytes) || row.bytes < 0) return null;
  return row.bytes;
}
