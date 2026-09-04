/**
 * Pure attempt-aware grouping for tool rounds and their narration segments.
 *
 * Responsibility: derive stable presentation batches without reading DOM or
 * browser state. Entry points are `computeExecutionBatches`,
 * `computeToolBatches`, `presentToolExecutionPanel`, `toolParentCallId`, and
 * the `toolGroupRound*` presentation helpers.
 * Dependencies: none; callers provide Conversation Sync projection values.
 */

type UnknownRecord = Readonly<Record<string, unknown>>;

export interface ToolExecutionBatch<Value> {
  readonly key: string;
  readonly baseKey: string;
  readonly items: Value[];
  readonly scope: string;
  readonly llmRound: number | string | null;
  readonly attemptOrdinal: number;
  readonly totalAttempts: number;
}

export interface ToolRoundBatch<Round> extends ToolExecutionBatch<Round> {
  readonly rounds: Round[];
}

export type ToolAttentionLevel =
  | 'routine'
  | 'important'
  | 'interactive'
  | 'error'
  | 'active';

export interface ToolAttentionSummary {
  readonly totalCount: number;
  readonly routineCount: number;
  readonly importantCount: number;
  readonly interactiveCount: number;
  readonly errorCount: number;
  readonly activeCount: number;
  readonly dominant: ToolAttentionLevel;
  readonly defaultCollapsed: boolean;
}

export interface ToolPanelPresentation {
  readonly active: boolean;
  readonly collapsed: boolean;
  readonly attention: ToolAttentionLevel;
  readonly html: string;
}

export type ToolPanelTranslator = (
  key: 'toolPanel.working' | 'toolPanel.toolsUsed'
    | 'toolPanel.turnsSuffix' | 'toolPanel.routineSummary',
  params: Readonly<{ n: number; s?: string }>,
) => string;

interface MutableExecutionBatch<Value> {
  key: string;
  baseKey: string;
  items: Value[];
  scope: string;
  llmRound: number | string | null;
  attemptOrdinal?: number;
  totalAttempts?: number;
}

export type ToolGroupTranslator = (
  key: 'toolPanel.roundTag' | 'ri.trTipAttempt',
  params: Readonly<{ n: number }>,
) => string;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

const ACTIVE_TOOL_STATUSES = new Set([
  'searching', 'executing', 'running', 'pending', 'queued',
]);
const ERROR_TOOL_STATUSES = new Set([
  'error', 'failed', 'rejected', 'aborted', 'interrupted',
  'cancelled', 'canceled',
]);
const INTERACTIVE_TOOL_STATUSES = new Set([
  'submitted', 'awaiting_input', 'awaiting_stdin', 'pending_approval',
]);
const ROUTINE_LEGACY_TOOL_VERBS = new Set([
  'read', 'search', 'find', 'list', 'get', 'grep', 'query', 'fetch',
  'inspect', 'view', 'screenshot', 'preview', 'status', 'time', 'weather',
]);

function toolField(value: unknown, key: string): unknown {
  const direct = record(value);
  const origin = originRecord(value);
  return direct?.[key] !== undefined ? direct[key] : origin?.[key];
}

function legacyRoutineToolName(value: unknown): boolean {
  const raw = toolField(value, 'toolName');
  if (typeof raw !== 'string' || !raw) return false;
  const leaf = raw.split('__').pop() ?? raw;
  const verb = leaf.toLowerCase().split(/[_-]/, 1)[0];
  return ROUTINE_LEGACY_TOOL_VERBS.has(verb)
    || leaf === 'read_files'
    || leaf === 'read_tool_artifact'
    || leaf === 'search_tool_artifact';
}

/**
 * Resolve the attention a tool row deserves without conflating semantics
 * (read versus write/approval) with transient runtime state.
 *
 * New rows use the contract-owned `attentionKind`. Legacy history falls back
 * conservatively: recognizable observation verbs are routine; unknown tools
 * stay important and therefore visible.
 */
export function toolRoundAttention(value: unknown): ToolAttentionLevel {
  const status = String(toolField(value, 'status') ?? '').toLowerCase();
  const programStatus = String(
    toolField(value, 'programStatus') ?? '',
  ).toLowerCase();
  if (Boolean(toolField(value, '_swarmActive'))
      || ACTIVE_TOOL_STATUSES.has(status)
      || ACTIVE_TOOL_STATUSES.has(programStatus)) return 'active';
  if (ERROR_TOOL_STATUSES.has(status)
      || ERROR_TOOL_STATUSES.has(programStatus)
      || Boolean(toolField(value, 'rejection'))
      || Boolean(toolField(value, '_rejected'))
      || Boolean(toolField(value, '_contractError'))) return 'error';

  const attentionKind = String(
    toolField(value, 'attentionKind') ?? '',
  ).toLowerCase();
  const toolName = String(toolField(value, 'toolName') ?? '');
  if (attentionKind === 'interactive'
      || INTERACTIVE_TOOL_STATUSES.has(status)
      || toolName === 'ask_human') return 'interactive';
  if (attentionKind === 'important'
      || Boolean(toolField(value, '_programSynthetic'))) return 'important';
  if (attentionKind === 'routine'
      || Boolean(toolField(value, 'parentToolCallId'))
      || Boolean(toolField(value, '_artifactOrigin'))
      || legacyRoutineToolName(value)) return 'routine';
  return 'important';
}

/** Summarize one panel/batch and decide whether it is safe to fold by default. */
export function summarizeToolAttention(
  values: readonly unknown[] | null | undefined,
): ToolAttentionSummary {
  const counts: Record<ToolAttentionLevel, number> = {
    routine: 0,
    important: 0,
    interactive: 0,
    error: 0,
    active: 0,
  };
  for (const value of values ?? []) counts[toolRoundAttention(value)] += 1;
  const dominant: ToolAttentionLevel = counts.active ? 'active'
    : counts.error ? 'error'
      : counts.interactive ? 'interactive'
        : counts.important ? 'important' : 'routine';
  const totalCount = values?.length ?? 0;
  return {
    totalCount,
    routineCount: counts.routine,
    importantCount: counts.important,
    interactiveCount: counts.interactive,
    errorCount: counts.error,
    activeCount: counts.active,
    dominant,
    defaultCollapsed: totalCount >= 4 && counts.routine === totalCount,
  };
}

/** Fold a noisy parallel observation batch while keeping small reads legible. */
export function shouldCollapseToolBatch(
  values: readonly unknown[] | null | undefined,
): boolean {
  const summary = summarizeToolAttention(values);
  return summary.totalCount >= 3 && summary.routineCount === summary.totalCount;
}

/**
 * Build the complete bounded panel header from typed attention facts.
 * Dynamic text crosses an explicit escaping port; `chevronHtml` is the
 * caller-owned trusted icon slot used by the retained composition adapter.
 */
export function presentToolExecutionPanel(
  rounds: readonly unknown[] | null | undefined,
  attentionRounds: readonly unknown[] | null | undefined,
  anyActive: boolean,
  translate: ToolPanelTranslator,
  escapeHtml: (value: string) => string,
  chevronHtml: string,
): ToolPanelPresentation {
  const attention = summarizeToolAttention(attentionRounds ?? rounds);
  const visible = summarizeToolAttention(rounds);
  const active = anyActive || attention.activeCount > 0;
  const collapsed = !active && attention.defaultCollapsed;
  const count = rounds?.length ?? 0;
  const turns = countToolTurns(rounds);
  const labelText = active
    ? translate('toolPanel.working', { n: count })
    : translate('toolPanel.toolsUsed', { n: count, s: count !== 1 ? 's' : '' })
      + (turns < count
        ? translate('toolPanel.turnsSuffix', {
          n: turns, s: turns !== 1 ? 's' : '',
        })
        : '');
  const label = `<span class="ptool-panel-label">${escapeHtml(labelText)}</span>`;
  const routine = visible.routineCount >= 3
    ? `<span class="ptool-panel-routine">${escapeHtml(translate(
      'toolPanel.routineSummary', { n: visible.routineCount },
    ))}</span>`
    : '';
  const html = active
    ? `<div class="ptool-panel-header">${label}${routine}</div>`
    : `<button type="button" class="ptool-panel-header" aria-expanded="${
      String(!collapsed)
    }">${label}${routine}<span class="ptool-panel-chevron">${
      chevronHtml
    }</span></button>`;
  return { active, collapsed, attention: attention.dominant, html };
}

/** Resolve the presentation-only parent without exposing storage identity. */
export function toolParentCallId(value: unknown): string {
  const artifactOrigin = record(toolField(value, '_artifactOrigin'));
  return firstTruthyIdentifier(
    toolField(value, 'parentToolCallId'),
    toolField(value, '_programCallId'),
    artifactOrigin?.sourceToolCallId,
  );
}

function originRecord(value: unknown): UnknownRecord | null {
  return record(record(value)?._round);
}

function firstTruthyIdentifier(...values: readonly unknown[]): string {
  const identifier = values.find(Boolean);
  return identifier === undefined ? '' : String(identifier);
}

function toolExecutionScope(value: unknown): string {
  const direct = record(value);
  const origin = originRecord(value);
  return firstTruthyIdentifier(
    direct?.attemptId,
    direct?._attemptId,
    direct?.taskId,
    direct?._taskId,
    origin?.attemptId,
    origin?._attemptId,
    origin?.taskId,
    origin?._taskId,
  );
}

/** Read the executor-local LLM round from a segment or its source round. */
export function toolExecutionLlmRound(value: unknown): number | string | null {
  const direct = record(value);
  const origin = originRecord(value);
  const raw = direct?.llmRound != null ? direct.llmRound : origin?.llmRound;
  return typeof raw === 'number' || typeof raw === 'string' ? raw : null;
}

function toolRoundOrdinal(value: unknown): number | null {
  const direct = record(value);
  const origin = originRecord(value);
  const raw = direct?.roundNum != null ? direct.roundNum : origin?.roundNum;
  return typeof raw === 'number' && Number.isInteger(raw) ? raw : null;
}

function executionValueHasTool(value: unknown): boolean {
  const direct = record(value);
  return direct?.type === 'tool_use' || Boolean(direct?.toolCallId);
}

function legacyExecutionRestart(
  previous: unknown,
  value: unknown,
  currentHasTool: boolean,
): boolean {
  if (toolExecutionScope(previous) || toolExecutionScope(value)) return false;
  const previousRound = toolRoundOrdinal(previous);
  const currentRound = toolRoundOrdinal(value);
  if (previousRound != null && currentRound != null && currentRound <= previousRound) {
    return true;
  }
  const direct = record(value);
  if (currentHasTool && !direct?.terminal
      && (direct?.type === 'text' || direct?.type === 'thinking')) return true;
  return Boolean(currentHasTool && currentRound == null
    && (direct?.assistantContent || direct?.thinking));
}

function toolBatchKey(value: unknown, hasLlm: boolean, position: number): string {
  const llmRound = toolExecutionLlmRound(value);
  const roundOrdinal = toolRoundOrdinal(value);
  const base = hasLlm && llmRound != null
    ? `L${llmRound}` : `S${roundOrdinal != null ? roundOrdinal : position}`;
  const scope = toolExecutionScope(value);
  return scope ? `A${scope}|${base}` : base;
}

function annotateExecutionAttempts<Value>(
  groups: MutableExecutionBatch<Value>[],
): ToolExecutionBatch<Value>[] {
  const scopeOrdinals = new Map<string, number>();
  let maximumOrdinal = 0;
  let legacyOrdinal = 0;
  let previous: MutableExecutionBatch<Value> | null = null;
  for (const group of groups) {
    if (group.scope) {
      if (!scopeOrdinals.has(group.scope)) {
        maximumOrdinal += 1;
        scopeOrdinals.set(group.scope, maximumOrdinal);
      }
      group.attemptOrdinal = scopeOrdinals.get(group.scope);
    } else {
      const reset = previous !== null && (Boolean(previous.scope)
        || (group.llmRound != null && previous.llmRound != null
          && group.llmRound <= previous.llmRound));
      if (!legacyOrdinal || reset) {
        maximumOrdinal += 1;
        legacyOrdinal = maximumOrdinal;
      }
      group.attemptOrdinal = legacyOrdinal;
    }
    previous = group;
  }
  const totalAttempts = new Set(groups.map((group) => group.attemptOrdinal)).size;
  return groups.map((group) => ({
    ...group,
    attemptOrdinal: group.attemptOrdinal ?? 0,
    totalAttempts,
  }));
}

/**
 * Group contiguous projection values by attempt and executor-local LLM round.
 * Legacy values without an attempt namespace split when their counters reset.
 */
export function computeExecutionBatches<Value>(
  values: readonly Value[] | null | undefined,
  hasLlm: boolean,
): ToolExecutionBatch<Value>[] {
  const orderedValues = values ?? [];
  const groups: MutableExecutionBatch<Value>[] = [];
  const occurrences = new Map<string, number>();
  let current: MutableExecutionBatch<Value> | null = null;
  let previous: Value | null = null;
  let currentHasTool = false;
  for (let index = 0; index < orderedValues.length; index += 1) {
    const value = orderedValues[index];
    const baseKey = toolBatchKey(value, hasLlm, index);
    const restart = current !== null && current.baseKey === baseKey
      && legacyExecutionRestart(previous, value, currentHasTool);
    if (current === null || current.baseKey !== baseKey || restart) {
      const occurrence: number = occurrences.get(baseKey) ?? 0;
      occurrences.set(baseKey, occurrence + 1);
      current = {
        key: occurrence ? `${baseKey}#${occurrence}` : baseKey,
        baseKey,
        items: [],
        scope: toolExecutionScope(value),
        llmRound: toolExecutionLlmRound(value),
      };
      groups.push(current);
      currentHasTool = false;
    }
    current.items.push(value);
    currentHasTool = currentHasTool || executionValueHasTool(value);
    previous = value;
  }
  return annotateExecutionAttempts(groups);
}

/** Group tool-round projections while retaining the legacy `rounds` alias. */
export function computeToolBatches<Round>(
  rounds: readonly Round[] | null | undefined,
): ToolRoundBatch<Round>[] {
  const values = rounds ?? [];
  const hasLlm = values.some((round) => toolExecutionLlmRound(round) != null);
  return computeExecutionBatches(values, hasLlm).map((group) => ({
    ...group,
    rounds: group.items,
  }));
}

/** Count represented model turns without claiming legacy parallelism. */
const _DISCRIM_VALUE_MAX = 24;

function _discrimArgs(round: unknown): UnknownRecord | null {
  const raw = record(round)?.toolArgs;
  if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as UnknownRecord;
  }
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as UnknownRecord;
      }
    } catch { /* malformed toolArgs — occurrence index still applies */ }
  }
  return null;
}

function _discrimValue(value: unknown): string {
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text) return '';
  return text.length > _DISCRIM_VALUE_MAX
    ? `${text.slice(0, _DISCRIM_VALUE_MAX - 1)}…` : text;
}

/**
 * Guarantee no two sibling rows in one batch ever render the SAME title.
 *
 * The backend composes each round's `query` per call, unaware of siblings;
 * a parallel batch can therefore legitimately contain several calls whose
 * composed titles collide (same tool, same resource, differing only in args
 * the title elided — or, for a future tool, args no key list knows about).
 * For every within-batch cluster of rounds sharing (toolName, query), derive
 * a suffix from the args that ACTUALLY differ across the cluster
 * (` · key=value`, at most two keys present in every sibling); when the args
 * are byte-equal too, fall back to the occurrence index (` #2`, ` #3`).
 * Keyed by durable toolCallId so callers never depend on object identity.
 */
export function siblingTitleDiscriminators(
  rounds: readonly unknown[] | null | undefined,
): ReadonlyMap<string, string> {
  const result = new Map<string, string>();
  const values = rounds ?? [];
  if (values.length < 2) return result;
  for (const batch of computeToolBatches(values)) {
    const clusters = new Map<string, unknown[]>();
    for (const round of batch.rounds) {
      const direct = record(round);
      const callId = direct?.toolCallId;
      const name = direct?.toolName;
      const query = direct?.query;
      if (typeof callId !== 'string' || !callId) continue;
      if (typeof name !== 'string' || !name) continue;
      if (typeof query !== 'string' || !query) continue;
      const key = `${name}${query}`;
      const cluster = clusters.get(key);
      if (cluster) cluster.push(round);
      else clusters.set(key, [round]);
    }
    for (const cluster of clusters.values()) {
      if (cluster.length < 2) continue;
      const argsList = cluster.map(_discrimArgs);
      const keyOrder: string[] = [];
      for (const args of argsList) {
        if (!args) continue;
        for (const key of Object.keys(args)) {
          if (!keyOrder.includes(key)) keyOrder.push(key);
        }
      }
      const rendered = argsList.map((args) => {
        const valuesByKey = new Map<string, string>();
        if (args) {
          for (const key of keyOrder) {
            const text = _discrimValue(args[key]);
            if (text) valuesByKey.set(key, text);
          }
        }
        return valuesByKey;
      });
      const diffKeys = keyOrder.filter((key) => {
        const distinct = new Set<string>();
        for (const valuesByKey of rendered) {
          const text = valuesByKey.get(key);
          if (text === undefined) return false;
          distinct.add(text);
        }
        return distinct.size > 1;
      }).slice(0, 2);
      const suffixes = cluster.map((round, index) => {
        if (!diffKeys.length) return index > 0 ? ` #${index + 1}` : '';
        const chips = diffKeys
          .map((key) => `${key}=${rendered[index].get(key) ?? ''}`);
        return ` · ${chips.join(' · ')}`;
      });
      /* Residual collisions: two siblings whose args agree on every diff key
       * (≤2 of possibly several differing keys) still render identically —
       * append the within-suffix occurrence index so the guarantee holds. */
      const seen = new Map<string, number>();
      cluster.forEach((round, index) => {
        let suffix = suffixes[index];
        const occurrence = (seen.get(suffix) ?? 0) + 1;
        seen.set(suffix, occurrence);
        if (occurrence > 1) suffix = `${suffix} #${occurrence}`;
        if (suffix) result.set(String(record(round)?.toolCallId), suffix);
      });
    }
  }
  return result;
}

export function countToolTurns<Round>(
  rounds: readonly Round[] | null | undefined,
): number {
  const values = rounds ?? [];
  const hasDirectLlmRound = values.some((round) => record(round)?.llmRound != null);
  return hasDirectLlmRound ? computeToolBatches(values).length : values.length;
}

/** Resolve the one-based API round represented by a presentation batch. */
export function toolGroupRoundNumber(group: unknown): number | null {
  const rounds = record(group)?.rounds;
  if (!Array.isArray(rounds) || rounds.length === 0) return null;
  const llmRound = record(rounds[0])?.llmRound;
  return typeof llmRound === 'number' && Number.isFinite(llmRound)
    ? llmRound + 1 : null;
}

/** Format a compact round identity without inventing legacy metadata. */
export function toolGroupRoundDisplay(group: unknown): string {
  const roundNumber = toolGroupRoundNumber(group);
  if (roundNumber == null) return '';
  const value = record(group);
  const attemptOrdinal = value?.attemptOrdinal;
  return typeof value?.totalAttempts === 'number' && value.totalAttempts > 1
    && typeof attemptOrdinal === 'number' && Number.isFinite(attemptOrdinal)
    ? `A${attemptOrdinal} · R${roundNumber}` : String(roundNumber);
}

/** Format the localized tooltip paired with `toolGroupRoundDisplay`. */
export function toolGroupRoundTitle(
  group: unknown,
  translate: ToolGroupTranslator,
): string {
  const roundNumber = toolGroupRoundNumber(group);
  if (roundNumber == null) return '';
  const roundLabel = translate('toolPanel.roundTag', { n: roundNumber });
  const value = record(group);
  const attemptOrdinal = value?.attemptOrdinal;
  return typeof value?.totalAttempts === 'number' && value.totalAttempts > 1
    && typeof attemptOrdinal === 'number' && Number.isFinite(attemptOrdinal)
    ? `${translate('ri.trTipAttempt', { n: attemptOrdinal })} · ${roundLabel}`
    : roundLabel;
}
