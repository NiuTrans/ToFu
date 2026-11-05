/** Read-only Trace presentation derived from the generated TurnTimingTrace contract.
 * Entry: projectInspectorTrace. Server clocks, coverage and aggregate buckets
 * remain authoritative; browser receipts never supply a missing server axis.
 * No DOM, transport, retained state, event folding or retention policy.
 */
import type {TurnTimingTrace, TurnTimingSpan, TurnTimingStatusEntry, TurnClientPerceptionObservation} from '../api/conversation-sync.generated';

export type TraceTranslate = (key: string, values?: Record<string, unknown>) => string;
export interface TraceBar {className: string; label: string; title: string; left: number; width: number}
export interface TraceRow {label: string; depth?: number; bars: TraceBar[]}
interface TraceNote {className: string; text: string}
interface TraceChip {className: string; label: string; value: string; detail: string}
export interface InspectorTraceProjection {
  availability: 'ready' | 'failed' | 'expired'; chips: TraceChip[]; notes: TraceNote[];
  rows: TraceRow[]; axis: string[]; statusText: string; statusRow: TraceRow | null; observations: string;
}
type Clock = {start: number; total: number};
const finite = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const records = <T>(value: unknown): T[] => Array.isArray(value) ? value.filter(record) as T[] : [];
const count = (value: unknown): number => Math.max(0, finite(value) ?? 0);

/** Compact diagnostic units differ from the conversation's mm:ss duration copy. */
export function traceDuration(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return '…';
  const ms = Math.max(0, Math.round(Number(value)));
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  const rounded = Math.round(seconds);
  if (rounded < 60) return `${rounded}s`;
  return `${Math.floor(rounded / 60)}m${String(rounded % 60).padStart(2, '0')}s`;
}
const KIND_KEYS: Readonly<Record<string, string>> = {
  turn: 'ri.trKindTurn', round: 'ri.trKindRound', llm: 'ri.trKindLlm', llm_ttft: 'ri.trKindLlmTtft',
  tool: 'ri.trKindTool', retry_wait: 'ri.trKindRetryWait', compaction: 'ri.trKindCompaction',
  approval_wait: 'ri.trKindApprovalWait', spawn_wait: 'ri.trKindSpawnWait', flow_step: 'ri.trKindFlowStep',
  startup: 'ri.trKindStartup', round_prep: 'ri.trKindRoundPrep', round_settle: 'ri.trKindRoundSettle', finalize: 'ri.trKindFinalize',
};
function spanTitle(span: TurnTimingSpan, elapsed: number, t: TraceTranslate): string {
  const key = Object.hasOwn(KIND_KEYS, span.kind) ? KIND_KEYS[span.kind] : undefined, translated = key ? t(key) : '';
  const kind = translated && translated !== key ? translated : String(span.kind || '');
  const lines = [`${span.name || span.kind} · ${kind} · ${traceDuration(elapsed)}`];
  const attrs = record(span.attrs) ? span.attrs : {};
  if (attrs.query) lines.push(String(attrs.query));
  if (attrs.model) lines.push(String(attrs.model));
  for (const attempt of records<Record<string, unknown>>(attrs.attempts)) {
    lines.push(`${attempt.tag || attempt.model || 'attempt'}: ${attempt.tokensIn ?? '…'}→${attempt.tokensOut ?? '…'} · ${traceDuration(attempt.streamElapsedMs)}`);
  }
  if (attrs.attempt != null) lines.push(t('ri.trTipAttempt', {n: attrs.attempt}));
  if (attrs.verdict) lines.push(String(attrs.verdict));
  if (span.budgetMs != null) lines.push(span.overBudget
    ? t('ri.trTipOverBudget', {budget: traceDuration(span.budgetMs), over: traceDuration(Math.max(0, elapsed - span.budgetMs))})
    : t('ri.trTipBudget', {budget: traceDuration(span.budgetMs)}));
  if (span.truncated) lines.push(t('ri.trTipTruncated'));
  if (span.status === 'running') lines.push(t('ri.trTipRunning'));
  return lines.join('\n');
}
function position(start: number, end: number, clock: Clock): {left: number; width: number} {
  const scale = Math.max(1, clock.total), clip = (value: number) => Math.min(100, Math.max(0, value));
  const left = clip((start - clock.start) / scale * 100), right = clip((end - clock.start) / scale * 100);
  return {left, width: Math.min(100 - left, Math.max(0.15, right - left))};
}
function spanRows(doc: Partial<TurnTimingTrace>, clock: Clock, t: TraceTranslate): TraceRow[] {
  const groups = new Map<number, TraceBar[]>();
  for (const span of records<TurnTimingSpan>(doc.spans)) {
    const start = finite(span.tStart) ?? clock.start, end = finite(span.tEnd) ?? clock.start + clock.total;
    const elapsed = Math.max(0, end - start), depth = Math.max(0, Math.floor(finite(span.depth) ?? 0));
    const className = 'tr-bar tr-k-' + (span.kind || 'unknown')
      + (span.status === 'error' || span.status === 'aborted' ? ' tr-err' : '')
      + (span.overBudget ? ' tr-over' : '') + (span.status === 'running' ? ' tr-live' : '')
      + (span.truncated ? ' tr-trunc' : '');
    const bars = groups.get(depth) ?? []; groups.set(depth, bars);
    bars.push({className, label: `${span.name || ''} ${traceDuration(elapsed)}`, title: spanTitle(span, elapsed, t), ...position(start, end, clock)});
  }
  const labels = ['ri.trRowTurn', 'ri.trRowPhase', 'ri.trRowDetail', 'ri.trRowSub'];
  const rows = [...groups].sort(([a], [b]) => a - b).map(([depth, bars]) => ({depth, bars,
    label: depth < labels.length ? t(labels[depth]) : `${t('ri.trRowSub')} ${depth}`}));
  const gaps = records<{tStart: number; tEnd: number}>(doc.gaps);
  if (gaps.length) rows.push({depth: -1, label: t('ri.trKindGap'), bars: gaps.map(gap => {
    const start = finite(gap.tStart) ?? clock.start, end = finite(gap.tEnd) ?? start;
    return {className: 'tr-bar tr-k-gap', label: '', title: `${t('ri.trKindGap')} · ${traceDuration(end - start)}`, ...position(start, end, clock)};
  })});
  return rows;
}
function summaryChips(doc: Partial<TurnTimingTrace>, t: TraceTranslate): TraceChip[] {
  const summary: Record<string, unknown> = record(doc.summary) ? doc.summary : {};
  const chips: TraceChip[] = [];
  const add = (kind: string, key: string, value: unknown, detail = '') => {
    if (value != null) chips.push({className: `tr-c-${kind}`, label: t(key), value: traceDuration(value), detail});
  };
  add('total', 'ri.traceTotal', doc.totalMs);
  add('llm', 'ri.trKindLlm', summary.llmMs, summary.ttftMs ? t('ri.traceTtftSub', {v: traceDuration(summary.ttftMs)}) : '');
  add('tool', 'ri.trKindTool', summary.toolMs);
  for (const [field, kind, key] of [
    ['waitMs', 'wait', 'ri.trKindRetryWait'], ['compactionMs', 'compact', 'ri.trKindCompaction'],
    ['approvalWaitMs', 'approval', 'ri.trKindApprovalWait'], ['flowStepMs', 'flowstep', 'ri.trKindFlowStep'],
  ]) if (summary[field]) add(kind, key, summary[field]);
  if (summary.orchestrationMs) add('orch', 'ri.trKindOrchestration', summary.orchestrationMs,
    t('ri.traceOrchestrationSub', {startup: traceDuration(summary.startupMs ?? 0), prep: traceDuration(summary.roundPrepMs ?? 0),
      settle: traceDuration(summary.roundSettleMs ?? 0), finalize: traceDuration(summary.finalizeMs ?? 0)}));
  if (summary.unattributedMs) add('gap', 'ri.trKindGap', summary.unattributedMs);
  return chips;
}
function traceNotes(doc: Partial<TurnTimingTrace>, clock: Clock | null, t: TraceTranslate): TraceNote[] {
  const notes: TraceNote[] = [], add = (text: string, tone = '') => notes.push({className: `tr-note${tone ? ' tr-note-' + tone : ''}`, text});
  if (doc.running) add(t('ri.traceLive'));
  if (doc.source === 'turn-snapshot') add(t('ri.traceDurable'));
  if (doc.source === 'attempt-receipts' && !clock) add(t('ri.traceReceiptsOnly'), 'warn');
  if (doc.coverage === 'partial') {
    const key = doc.coverageReason === 'flow' ? 'ri.tracePartialFlow'
      : doc.coverageReason === 'no-round-markers' ? 'ri.tracePartialLegacy' : 'ri.tracePartialEvidence';
    add(t(key, {reason: doc.coverageReason || 'unknown'}), 'warn');
  } else if (!clock && doc.source !== 'attempt-receipts') add(t('ri.tracePartialEvidence', {reason: 'clock-unavailable'}), 'warn');
  const over = records<{name: string; elapsedMs: number; budgetMs: number}>(doc.summary?.overBudget);
  if (over.length) add(`${t('ri.traceOverBudget', {n: over.length})}: ` + over.map(row => `${row.name} ${traceDuration(row.elapsedMs)} / ${traceDuration(row.budgetMs)}`).join(' · '), 'over');
  const dropped = [doc.droppedSpans, doc.droppedGaps, doc.statusDroppedCount, doc.clientObservationDroppedCount, doc.overBudgetDroppedCount].reduce<number>((sum, value) => sum + count(value), 0);
  if (doc.compacted || dropped) add(t('ri.traceCompacted', {n: dropped}), 'warn');
  const clientDropped = records<TurnClientPerceptionObservation>(doc.clientObservations).reduce((maximum, row) => Math.max(maximum, count(row.clientDroppedBefore)), 0);
  if (clientDropped) add(t('ri.traceClientDropped', {n: clientDropped}), 'warn');
  return notes;
}
function statusLabel(status: TurnTimingStatusEntry, t: TraceTranslate): string {
  const key = status.detailKey || '', translated = key ? t(key, status.detailArgs) : '';
  return translated && translated !== key ? translated : String(status.detail || status.phase || '');
}
function statuses(doc: Partial<TurnTimingTrace>, clock: Clock | null, t: TraceTranslate): {statusText: string; statusRow: TraceRow | null} {
  const text: string[] = [], bars: TraceBar[] = [];
  for (const status of records<TurnTimingStatusEntry>(doc.statusHistory)) {
    const start = finite(status.tStart) ?? clock?.start ?? null;
    const end = finite(status.tEnd) ?? finite(status.lastObservedAt) ?? (clock ? clock.start + clock.total : null);
    const elapsed = start != null && end != null ? Math.max(0, end - start) : null, label = statusLabel(status, t);
    text.push(`${label} ${traceDuration(elapsed)}` + (Number(status.count) > 1 ? ` ${t('ri.traceStatusRepeated', {n: status.count})}` : ''));
    if (clock && start != null && end != null) bars.push({label, title: `${label} · ${traceDuration(elapsed)}`,
      className: status.attention === 'stall' ? 'tr-bar tr-k-retry_wait tr-err'
        : status.attention === 'wait' ? 'tr-bar tr-k-retry_wait' : 'tr-bar tr-k-round', ...position(start, end, clock)});
  }
  return {statusText: text.join(' → '), statusRow: bars.length ? {label: t('ri.tracePromptRow'), bars} : null};
}
function observationLabel(row: TurnClientPerceptionObservation, t: TraceTranslate): string {
  if ((row.kind === 'phase_painted' || row.kind === 'terminal_painted') && row.renderMs == null) return t('ri.tracePaintRestored');
  if (row.kind === 'phase_painted') return t('ri.tracePhasePainted', {phase: row.phase || row.detailKey || '—', render: traceDuration(row.renderMs)});
  if (row.kind === 'terminal_painted') return t('ri.traceTerminalPainted', {render: traceDuration(row.renderMs)});
  if (row.kind === 'transport_degraded') return t('ri.traceTransportDegraded', {state: row.healthState || 'degraded', reason: row.reason || '—'});
  if (row.kind === 'transport_recovered') return t('ri.traceTransportRecovered', {duration: traceDuration(row.durationMs)});
  return String(row.kind || '');
}
export function projectInspectorTrace(data: unknown, t: TraceTranslate): InspectorTraceProjection {
  const empty: InspectorTraceProjection = {availability: 'failed', chips: [], notes: [], rows: [], axis: [], statusText: '', statusRow: null, observations: ''};
  if (!record(data) || data.eventsAvailable !== true) return {...empty, availability: record(data) && data.eventsAvailable === false ? 'expired' : 'failed'};
  const doc = data as Partial<TurnTimingTrace>, start = finite(doc.tStart), total = finite(doc.totalMs);
  const clock = start != null && start >= 0 && total != null && total >= 0 && record(doc.summary) ? {start, total} : null;
  const observations = records<TurnClientPerceptionObservation>(doc.clientObservations).map(row => observationLabel(row, t)
    + (row.transportMs != null ? ` · ${t('ri.traceClientTransport', {duration: traceDuration(row.transportMs)})}` : '')).join(' · ');
  return {availability: 'ready', chips: summaryChips(doc, t), notes: traceNotes(doc, clock, t),
    rows: clock ? spanRows(doc, clock, t) : [], axis: clock ? ['0', traceDuration(total! / 2), traceDuration(total)] : [],
    ...statuses(doc, clock, t), observations};
}
