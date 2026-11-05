/**
 * Report/Review event projection: chat-shaped segments, tool evidence and
 * report-specific postpasses. No DOM, task loop or global state. The retained
 * presenter supplies view/publication ports; report-runtime owns transport.
 */
import type { TurnToolRound } from '../../api/conversation-sync.generated';
import type { PaperPushEvent, PaperPushState } from './push-transport';

export type ReportMeta = Record<string, unknown>;
export type ReportSegment =
  | { type: 'thinking' | 'text'; text: string; llmRound: number }
  | { type: 'tool_use'; id: string; llmRound: number };

export type ReportToolRound = Omit<TurnToolRound, 'results' | 'attentionKind'> & {
  results?: TurnToolRound['results'] | null;
  attentionKind?: string;
  _partialOutput?: string;
  _partialOutputSeq?: number;
  _partialOutputTotalChars?: number;
  _partialOutputTruncated?: boolean;
};

/** Report producer fields consumed here; not a new wire schema. Opaque tool
 * results and postpass metadata retain their producer-defined contents. */
export interface ReportEvent extends PaperPushEvent {
  llmRound?: number;
  roundNum?: number;
  status?: string;
  delta?: string;
  toolName?: string;
  query?: string;
  toolCallId?: string;
  attentionKind?: string;
  parentToolCallId?: string;
  results?: ReportToolRound['results'];
  text?: string;
  paperHash?: string;
  insight?: string;
  addendum?: string;
  report?: string;
  meta?: ReportMeta;
  resolvedTitle?: string;
}

export interface ReportStreamState extends PaperPushState {
  paperId: string;
  lang: string;
  kind: string;
  taskId: string;
  cursor: number;
  status: string;
  pendingStop: boolean;
  stopRequested: boolean;
  fullText: string;
  reportBody: string | null;
  workerActive: boolean;
  thinkingText: string;
  toolRounds: ReportToolRound[];
  segments: ReportSegment[];
  _segInferRound: number;
  contentStarted: boolean;
  insightText: string;
  _insightRunning: boolean;
  _insightApplied: boolean;
  termfillText: string;
  _termfillApplied: boolean;
  meta: ReportMeta | null;
  error: string;
  _errorEnv?: unknown;
  pollTimer: ReturnType<Window['setTimeout']> | null;
  pollBusy: boolean;
  _lastRenderedLen: number;
  _lastRenderedStatus: string;
  _lastToolKey: string;
}

export interface ReportEventView {
  cache: string;
  meta: ReportMeta | null;
  langKey(): string;
}

type PostpassHandler = (
  state: ReportStreamState, event: ReportEvent,
) => boolean;

export interface ReportEventPorts {
  isActivePaper(paperId: string): boolean;
  viewFor(kind: string): ReportEventView;
  setPaperHash(hash: string): void;
  rememberSnapshot(view: ReportEventView, text: string, meta: ReportMeta | null): void;
  persistReviewVenue(view: ReportEventView, lang: string, paperId: string): void;
  applyResolvedTitle(title: string, paperId: string): void;
  handleInsight?: PostpassHandler;
  handleCheckpoints?: PostpassHandler;
  normalizeError?: (error: unknown) => unknown;
  errorMessage?: (error: unknown) => string;
}

export function makeReportStreamState(
  paperId = '',
  lang = 'en',
  taskId = '',
  kind = 'report',
): ReportStreamState {
  return {
    paperId,
    lang,
    kind,
    taskId,
    cursor: 0,
    status: 'running',
    pendingStop: false,
    stopRequested: false,
    fullText: '',
    reportBody: null,
    workerActive: false,
    thinkingText: '',
    toolRounds: [],
    segments: [],
    _segInferRound: 0,
    contentStarted: false,
    insightText: '',
    _insightRunning: false,
    _insightApplied: false,
    termfillText: '',
    _termfillApplied: false,
    meta: null,
    error: '',
    pollTimer: null,
    pollBusy: false,
    _lastRenderedLen: -1,
    _lastRenderedStatus: '',
    _lastToolKey: '',
  };
}

/** Reconcile the primary snapshot with separately persisted legacy addenda. */
export function setReportBody(state: ReportStreamState, body: string): string {
  if (state.reportBody === body) return state.fullText;
  state.reportBody = body;
  let text = body;
  for (const addendum of [state.insightText, state.termfillText]) {
    if (addendum) text = text.replace(/\s*$/, '') + '\n\n' + addendum + '\n';
  }
  if (state.fullText !== text) state._lastRenderedLen = -1;
  state.fullText = text;
  return text;
}

function appendReportAddendum(state: ReportStreamState, addendum: string): void {
  state.reportBody ??= state.fullText;
  state.fullText = state.fullText.replace(/\s*$/, '') + '\n\n' + addendum + '\n';
  state._lastRenderedLen = -1;
}

function segmentRound(state: ReportStreamState, event: ReportEvent): number {
  return (typeof event.llmRound === 'number') ? event.llmRound : state._segInferRound;
}

function appendProse(
  state: ReportStreamState, type: 'thinking' | 'text', delta: string, llmRound: number,
): void {
  if (!delta) return;
  const last = state.segments[state.segments.length - 1];
  if (last && last.type !== 'tool_use' && last.type === type && last.llmRound === llmRound) {
    last.text += delta;
  } else {
    state.segments.push({ type, text: delta, llmRound });
  }
}

function appendToolStart(state: ReportStreamState, event: ReportEvent): void {
  const r = segmentRound(state, event);
  state.segments.push({ type: 'tool_use', id: event.toolCallId || '', llmRound: r });
  // Prose that arrives after this tool belongs to the NEXT dispatch round.
  state._segInferRound = r + 1;
}

function resetDraft(state: ReportStreamState, event: ReportEvent): void {
  // The backend discards a tool round's interim DRAFT (the terminal round
  // rewrites the whole report), so its narration segment goes with it — a
  // discarded draft must not linger next to the tools. Thinking is never
  // reset, so thinking segments are kept.
  const r = segmentRound(state, event);
  state.segments = state.segments.filter(function (seg) {
    return !(seg.type === 'text' && seg.llmRound === r);
  });
}

/** Segments for the timeline render. Text (narration) segments render ONLY
 *  for rounds that actually called tools — the terminal tool-less round's
 *  text IS the report body, which the caller renders separately below the
 *  panel; letting it into the timeline would double it (mirrors the chat
 *  timeline, which skips `deliverable` segments). */
export function reportSegmentsForRender(state: ReportStreamState): ReportSegment[] {
  const withTools = new Set<number>();
  for (const segment of state.segments) {
    if (segment.type === 'tool_use') withTools.add(segment.llmRound);
  }
  return state.segments.filter(segment =>
    segment.type !== 'text' || withTools.has(segment.llmRound));
}

/* ── Versioned tool_progress replay safety (tofu.tool-progress/v1) ───────
 * ``tool_progress`` carries a monotonic per-call ``seq`` (1-based) alongside
 * the ordered ``chunk`` delta. The shared task-level seq gate
 * already dedupes whole events across the push/poll transports; this gate is
 * the per-call defense — it rejects a duplicate or out-of-order chunk for the
 * same round so the bounded partial-output buffer never double-appends. A
 * legacy frame without seq still applies unconditionally. */
const TOOL_PROGRESS_BOUNDED_CHARS = 100000;
const TOOL_PROGRESS_TRUNCATION_MARKER =
  '\n\n… [live output truncated] …\n\n';
const TOOL_PROGRESS_TRUNCATION_TAIL_RE =
  /… \[live output truncated[^\]]*\] …\n\n([\s\S]*)$/;

function progressTail(value: unknown): string {
  const existing = typeof value === 'string' ? value : '';
  const match = TOOL_PROGRESS_TRUNCATION_TAIL_RE.exec(existing);
  return match ? match[1] : existing;
}

function progressStatus(truncated: boolean, spooling: boolean, reason: string): string {
  if (reason === 'cancelling') return 'cancelling';
  if (reason === 'cancelled' || reason === 'cancelled-partial') {
    return 'cancelled-partial';
  }
  if (truncated) return 'overflow-loss';
  if (spooling) return 'spooling';
  return 'running';
}

function applyToolProgress(round: ReportToolRound, ev: ReportEvent): boolean {
  const seq = typeof ev.seq === 'number' ? ev.seq : null;
  const lastSeq = typeof round._partialOutputSeq === 'number'
    ? round._partialOutputSeq : 0;
  if (seq !== null && seq <= lastSeq) return false;

  const chunk = typeof ev.chunk === 'string' ? ev.chunk : '';
  const existingTotal = typeof round._partialOutputTotalChars === 'number'
    ? round._partialOutputTotalChars
    : progressTail(round._partialOutput).length;
  const totalChars = existingTotal + chunk.length;
  const truncated = round._partialOutputTruncated === true
    || ev.truncated === true
    || totalChars > TOOL_PROGRESS_BOUNDED_CHARS;
  const spooling = ev.spooling === true;
  const reason = typeof ev.terminalReason === 'string' ? ev.terminalReason : '';

  if (!truncated) {
    round._partialOutput = (typeof round._partialOutput === 'string'
      ? round._partialOutput : '') + chunk;
  } else {
    const suffixLimit = TOOL_PROGRESS_BOUNDED_CHARS
      - Math.floor(TOOL_PROGRESS_BOUNDED_CHARS * 3 / 4);
    const tail = (progressTail(round._partialOutput) + chunk)
      .slice(-suffixLimit);
    round._partialOutput = TOOL_PROGRESS_TRUNCATION_MARKER + tail;
  }
  round._partialOutputTotalChars = totalChars;
  round._partialOutputTruncated = truncated;
  round._partialOutputStatus = progressStatus(truncated, spooling, reason);
  if (seq !== null) round._partialOutputSeq = seq;
  if (ev.contractVersion != null) {
    round._partialOutputContractVersion = String(ev.contractVersion);
  }
  if (ev.stream != null) round._partialOutputStream = String(ev.stream);
  if (spooling) round._partialOutputSpooling = true;
  if (reason) round._partialOutputTerminalReason = reason;
  if (ev.grepSearchIntercepted === true) round.grepSearchIntercepted = true;
  return true;
}

/** Apply a single event to the in-memory stream state. Returns dirty flag. */
export function createReportEventProjection(ports: ReportEventPorts) {
  return function applyReportEventRaw(s: ReportStreamState, ev: ReportEvent): boolean {
    switch (ev.type) {
      case 'status':
        s.status = ev.status || s.status;
        return true;

      case 'thinking':
        s.thinkingText += (ev.delta || '');
        appendProse(s, 'thinking', ev.delta || '', segmentRound(s, ev));
        return true;

      case 'tool_start': {
        // Chat-compatible round entry
        s.toolRounds.push({
          roundNum: ev.roundNum,
          toolName: ev.toolName,
          query: ev.query || ev.toolName,
          toolCallId: ev.toolCallId || '',
          toolArgs: ev.toolArgs || '',
          attentionKind: ev.attentionKind || '',
          parentToolCallId: ev.parentToolCallId || '',
          status: 'searching',
          results: null,
        });
        appendToolStart(s, ev);
        return true;
      }

      case 'tool_done': {
        const r = s.toolRounds.find(round => round.roundNum === ev.roundNum);
        if (r) {
          r.status = 'done';
          if (typeof ev.elapsed === 'number') r._elapsed = ev.elapsed.toFixed(1) + 's';
          if (ev.toolContent) r.toolContent = ev.toolContent;
          if (ev.results) r.results = ev.results;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
          if (ev.vertical) r.vertical = ev.vertical;
          if (ev.verticals) r.verticals = ev.verticals;
          if (ev.cacheSource) r.cacheSource = ev.cacheSource;
        }
        return true;
      }

      case 'tool_progress': {
        const rp = s.toolRounds.find(round => round.roundNum === ev.roundNum);
        if (rp) {
          applyToolProgress(rp, ev);
        }
        return true;
      }

      case 'delta':
        s.fullText += (ev.delta || '');
        s.contentStarted = true;
        appendProse(s, 'text', ev.delta || '', segmentRound(s, ev));
        return true;

      case 'delta_reset':
        // The model emitted an interim draft alongside a tool call; the
        // backend discards it and will rewrite the full report after the tool
        // results land. Clear the accumulated text so the draft + final report
        // don't concatenate (report rendered twice).
        s.fullText = '';
        s.contentStarted = false;
        s._lastRenderedLen = -1;
        resetDraft(s, ev);
        return true;

      case 'enriched':
        s.fullText = ev.text || s.fullText;
        // Only mutate global hash when this stream still belongs to the active paper —
        // a stream that was started for paper A and is now polling in the background
        // must not stomp paper B's hash.
        if (ev.paperHash && ports.isActivePaper(s.paperId)) ports.setPaperHash(ev.paperHash);
        return true;

      case 'insight_start':
        // The gated insight second-pass has begun (after the report `done`).
        // Flag it so the reader can show a subtle "synthesizing insight…" hint;
        // no body change yet.
        s._insightRunning = true;
        return true;

      case 'insight': {
        // v2 structured payload (grounded items with resolved anchor_idx) →
        // the reading-xp rail distributes anchored cards instead of appending
        // one end-of-report block. Legacy events (no items) fall through to
        // the markdown-append path below.
        if (ports.handleInsight && ports.handleInsight(s, ev)) {
          return true;
        }
        // The insight pass produced a grounded synthesis/transfer section. It is
        // a self-contained Markdown block (## 💡 …) persisted separately under
        // the `insight:<ui>` key; render it live by appending to the report body
        // (and the cached snapshot) so it flows through the same markdown
        // renderer + TOC. Guard against double-append on cursor replay.
        s._insightRunning = false;
        const _ins = ev.insight || '';
        if (_ins && !s._insightApplied) {
          s._insightApplied = true;
          s.insightText = _ins;
          appendReportAddendum(s, _ins);
          if (ports.isActivePaper(s.paperId)) {
            const _vIns = ports.viewFor(s.kind);
            if (_vIns.cache) {
              _vIns.cache = s.fullText;
              ports.rememberSnapshot(_vIns, _vIns.cache, _vIns.meta || s.meta);
            }
          }
        }
        return true;
      }

      case 'insight_skipped':
        // Gate withheld (report already insight-saturated) or nothing produced.
        // No body change; just clear the running hint.
        s._insightRunning = false;
        return true;

      case 'checkpoints':
        // Per-section self-test flip cards (P2) — the reading-xp rail inserts
        // them at section ends. No body change.
        if (ports.handleCheckpoints && ports.handleCheckpoints(s, ev)) {
          return true;
        }
        return true;

      case 'checkpoints_skipped':
        return true;

      case 'report_meta':
        // Metadata follows the same active-view ownership as completion.
        // The article presenter reconciles it for both events and snapshots.
        if (ev.meta) {
          s.meta = ev.meta;
          const _vRm = ports.viewFor(s.kind);
          if (ports.isActivePaper(s.paperId)) _vRm.meta = ev.meta;
        }
        return true;

      case 'termfill': {
        // The definition-backfill pass produced a gap-closing glossary addendum
        // (a `## 📖 … (glossary backfill)` table), persisted separately under the
        // `termfill:<ui>` key. Append it live so the reader sees the added
        // definitions, and — because the re-audit proved the gaps are closed —
        // downgrade the warning card: clear meta.terminologyAudit so
        // _renderFinalReport stops rendering it (the glossary is now complete).
        // Guard against double-append on cursor replay.
        const _add = ev.addendum || '';
        if (_add && !s._termfillApplied) {
          s._termfillApplied = true;
          s.termfillText = _add;
          appendReportAddendum(s, _add);
          if (s.meta && s.meta.terminologyAudit) s.meta.terminologyAudit = null;
          s._lastRenderedLen = -1;   // force re-render (drops the warning card)
          if (ports.isActivePaper(s.paperId)) {
            const _vTf = ports.viewFor(s.kind);
            if (_vTf.meta && _vTf.meta.terminologyAudit) _vTf.meta.terminologyAudit = null;
            if (_vTf.cache) {
              _vTf.cache = s.fullText;
              ports.rememberSnapshot(_vTf, _vTf.cache, _vTf.meta || s.meta);
            }
          }
        }
        return true;
      }

      case 'termfill_skipped':
        // No gap-closing addendum produced — the warning card stays as-is.
        return true;

      case 'done': {
        s.status = 'done';
        const _vDone = ports.viewFor(s.kind);
        if (ev.report) {
          setReportBody(s, ev.report);
          if (ports.isActivePaper(s.paperId)) {
            _vDone.cache = s.fullText;
            ports.rememberSnapshot(_vDone, s.fullText, ev.meta || s.meta);
            ports.persistReviewVenue(_vDone, _vDone.langKey(), s.paperId);
          }
        }
        if (ev.meta) {
          s.meta = ev.meta;
          if (ports.isActivePaper(s.paperId)) _vDone.meta = ev.meta;
        }
        if (ev.paperHash && ports.isActivePaper(s.paperId)) ports.setPaperHash(ev.paperHash);
        if (ev.resolvedTitle) ports.applyResolvedTitle(ev.resolvedTitle, s.paperId);
        return true;
      }

      case 'aborted':
        s.status = 'aborted';
        // Keep whatever partial text was produced so the user sees how far the
        // model got before they stopped it. The frontend renders it read-only
        // under a "stopped" banner (never persisted / cached).
        if (typeof ev.partial === 'string' && ev.partial) {
          s.fullText = ev.partial;
          s.contentStarted = true;
        }
        return true;

      case 'error':
        s.status = 'error';
        // ev.error is a typed error envelope dict from routes/paper.py.
        // Display surfaces use the short ``message`` field; keep the full
        // envelope on s._errorEnv for future kind-aware rendering.
        s._errorEnv = (typeof ports.normalizeError === 'function')
          ? ports.normalizeError(ev.error)
          : null;
        s.error = (typeof ports.errorMessage === 'function'
                   ? ports.errorMessage(ev.error) : '')
                  || (typeof ev.error === 'string' ? ev.error : '')
                  || 'Unknown error';
        return true;
    }
    return false;
  };
}
