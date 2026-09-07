// @ts-check
/* Generated lazy retained runtime: paper-reader-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { _i18nLang, t } from '../i18n/index';
import { escapeHtml } from '../html-safety';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('paper-reader-presenters runtime dependency is unavailable: Api');
const Icon = runtimeScope.Icon;
if (typeof Icon !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: Icon');
const debugLog = runtimeScope.debugLog;
if (typeof debugLog !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: debugLog');
const pushSubscribe = runtimeScope.pushSubscribe;
if (typeof pushSubscribe !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: pushSubscribe');
const pushUnsubscribe = runtimeScope.pushUnsubscribe;
if (typeof pushUnsubscribe !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: pushUnsubscribe');
const renderMarkdown = runtimeScope.renderMarkdown;
if (typeof renderMarkdown !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: renderMarkdown');
const renderToolRoundsHTML = runtimeScope.renderToolRoundsHTML;
if (typeof renderToolRoundsHTML !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: renderToolRoundsHTML');
const showToast = runtimeScope.showToast;
if (typeof showToast !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: showToast');
const _openImageFullscreen = runtimeScope._openImageFullscreen;
if (typeof _openImageFullscreen !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: _openImageFullscreen');
const normalizeErrorEnvelope = runtimeScope.normalizeErrorEnvelope;
if (typeof normalizeErrorEnvelope !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: normalizeErrorEnvelope');
const errorEnvelopeMessage = runtimeScope.errorEnvelopeMessage;
if (typeof errorEnvelopeMessage !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: errorEnvelopeMessage');
const prefersReducedMotion = runtimeScope.prefersReducedMotion;
if (typeof prefersReducedMotion !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: prefersReducedMotion');
const formatCny = runtimeScope.formatCny;
if (typeof formatCny !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: formatCny');
const renderSegmentTimelineHTML = runtimeScope.renderSegmentTimelineHTML;
if (typeof renderSegmentTimelineHTML !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: renderSegmentTimelineHTML');
const _compareModelsByDisplayName = runtimeScope._compareModelsByDisplayName;
if (typeof _compareModelsByDisplayName !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: _compareModelsByDisplayName');
const _modelShortName = runtimeScope._modelShortName;
if (typeof _modelShortName !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: _modelShortName');
const _paperModelPickerState = runtimeScope._paperModelPickerState;
if (typeof _paperModelPickerState !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: _paperModelPickerState');
const apiUrl = runtimeScope.apiUrl;
if (typeof apiUrl !== 'function') throw new Error('paper-reader-presenters runtime dependency is unavailable: apiUrl');
/* ===== migrated source: paper/report.js ===== */
/* ═════════════════════════════════════════════════
   paper/report.js — Report + Review Mode (server-owned task + poll + render).

   Extracted verbatim from frontend/src/runtime/paper-reader.js (2026-07-11, Epic E
   cut #6 — the biggest, ~2400 L). The Tab-2 Report AND Review-Mode stack:
   event apply/paint (parameterized by _reportView `kind`), model+venue
   dropdowns, glossary cards, zoomable figures, export. The typed report owner
   owns start/restart, polling, reopen, preferences, snapshots, and anchors.
   Plus the 7 load-time document.addEventListener wirings (glossary
   hover, dropdown-close, figure zoom click/keydown, venue-close) — all only
   CALL document.addEventListener at load; their bodies run at runtime.
   Retained renderer state (_paperReportCache/_paperReviewCache/_paperReportStream
   /_paperReportMeta/_paperReportModel/_paperImages/_paperHash)
   STAYS in the core State block (read across many clusters). Only functions +
   listeners move. Window-scope var + runtime cross-refs → loads before paper-reader.js.
   ═════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Tab 2: Report — server-owned background task + polling
// ══════════════════════════════════════════════════════
//
// ARCHITECTURE (2026-04-18 rewrite)
//   • Reports are generated EXACTLY ONCE on the server per (paper_hash, lang).
//     On completion the enriched report is persisted to `paper_reports`.
//   • The frontend is purely a progress renderer. It never owns report state.
//   • Flow:
//       POST /api/paper/report/start  → {task_id} (or {cached, report} if DB hit)
//       GET  /api/paper/report/poll?task_id=X&cursor=N → {events, next_cursor, …}
//   • On tab/mode switch, the typed owner resolves live work plus preferred /
//     fallback cache through one /api/paper/report/lookup request, then resumes
//     polling from cursor=0 when a live task wins.
//   • Tool-round events (tool_start / tool_done) use the same schema as
//     chat tool events, so `renderToolRoundsHTML(toolRounds)` from ui.js
//     renders them identically to how they look in the chat bubble.

/** Skeleton DOM that gets populated by event application. The inner element
 *  ids are PREFIXED per view (report→`reportToolZone`, review→`reviewToolZone`)
 *  so a report skeleton and a review skeleton can coexist in the DOM (only one
 *  tab is visible at a time, but both containers persist) without
 *  getElementById collisions. */
function _renderReportSkeleton(container, lang, view) {
  view = view || _reportView('report');
  var px = view.idPrefix;
  var genTxt = view.kind === 'review'
    ? (lang === 'zh' ? '正在生成评审…' : 'Generating review…')
    : (lang === 'zh' ? '正在生成报告…' : 'Generating report…');
  container.innerHTML =
    '<div class="paper-report-tools" id="' + px + 'ToolZone"></div>' +
    '<details class="paper-report-thinking" id="' + px + 'ThinkingBlock" open style="display:none">' +
      '<summary><span class="thinking-dot"></span>' +
        (lang === 'zh' ? '思考中…' : 'Thinking…') +
      '</summary>' +
      '<div class="paper-report-thinking-body" id="' + px + 'ThinkingBody"></div>' +
    '</details>' +
    '<div class="paper-report-body" id="' + px + 'BodyContent">' +
      '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' +
        genTxt +
      '</div></div>' +
    '</div>';
}

/** Segment-timeline builders (chat-shaped). The report engine emits its
 *  thinking / delta / tool events in strict order (seq-cursor polling), each
 *  tagged with the 0-based dispatch round that produced it (`llmRound`).
 *  Folding them into per-round segments is what lets the report stream render
 *  through the chat inline tool timeline instead of the old three-zone layout.
 *  Events lacking `llmRound` (an older server mid-upgrade) fall back to
 *  order-based inference — thinking/delta before a tool_start belong to the
 *  round that tool_start opens. */
function _segRoundOf(s, ev) {
  return (typeof ev.llmRound === 'number') ? ev.llmRound : s._segInferRound;
}

function _segAppendProse(s, type, delta, llmRound) {
  if (!delta) return;
  var last = s.segments[s.segments.length - 1];
  if (last && last.type === type && last.llmRound === llmRound) {
    last.text += delta;
  } else {
    s.segments.push({ type: type, text: delta, llmRound: llmRound });
  }
}

function _segApplyToolStart(s, ev) {
  var r = _segRoundOf(s, ev);
  s.segments.push({ type: 'tool_use', id: ev.toolCallId || '', llmRound: r });
  // Prose that arrives after this tool belongs to the NEXT dispatch round.
  s._segInferRound = r + 1;
}

function _segApplyDeltaReset(s, ev) {
  // The backend discards a tool round's interim DRAFT (the terminal round
  // rewrites the whole report), so its narration segment goes with it — a
  // discarded draft must not linger next to the tools. Thinking is never
  // reset, so thinking segments are kept.
  var r = _segRoundOf(s, ev);
  s.segments = s.segments.filter(function (seg) {
    return !(seg.type === 'text' && seg.llmRound === r);
  });
}

/** Segments for the timeline render. Text (narration) segments render ONLY
 *  for rounds that actually called tools — the terminal tool-less round's
 *  text IS the report body, which the caller renders separately below the
 *  panel; letting it into the timeline would double it (mirrors the chat
 *  timeline, which skips `deliverable` segments). */
function _reportSegmentsForRender(s) {
  var withTools = {};
  var i, seg;
  for (i = 0; i < s.segments.length; i++) {
    seg = s.segments[i];
    if (seg.type === 'tool_use') withTools[seg.llmRound] = true;
  }
  return s.segments.filter(function (sg) {
    return sg.type !== 'text' || withTools[sg.llmRound];
  });
}

/** Bind the 'paper' push channel to a report stream ().
 *
 * ── The asymmetry this closes ──
 * The BACKEND has always been ready: ``lib/paper/report_runtime.py`` builds its
 * TaskRuntime with ``push_channel='paper'``, so every ``_append_report_event``
 * is broadcast on the unified /api/push WebSocket the moment it is appended,
 * and ``report_engine._execute_tool`` appends ``tool_done`` the instant the
 * tool returns. The report view simply never listened: its only transport was
 * ``setTimeout(runtimeScope._pollReportTask, 1200)``. So a search that finished at t=0 kept
 * its spinner until somewhere in t+1.2s…t+3s for no reason other than that
 * nobody subscribed to the channel already carrying the news.
 *
 * The native Paper research runtime uses the same mechanism;
 * this is the same layering: push is the ACCELERATOR, the poll stays as the
 * floor (a WS-blocked client must still converge, so the poll is never
 * removed).
 *
 * De-duplication between the two transports is the seq gate in
 * ``_applyReportEvent`` — this function does not need to coordinate with the
 * poll beyond handing frames to the same gate.
 *
 * Safe to call repeatedly for the same stream (idempotent per task id).
 */
function _attachReportPush(view, s) {
  if (!view || !s || !s.taskId) return;
  if (typeof pushSubscribe !== 'function') return;   // push module not loaded
  if (s._pushTaskId === s.taskId) return;            // already bound
  _detachReportPush(s);
  var taskId = s.taskId;
  try {
    pushSubscribe('paper', taskId, function (ev) {
      // Ignore frames for a stream this view has since replaced (paper switch,
      // regenerate) — the same abandon guard the poll chain uses.
      if (view.stream !== s) return;
      if (!ev || !ev.type) return;
      var dirty = _applyReportEvent(s, ev);

      // Terminal frames settle the view AND release the subscription, so a
      // long session does not accumulate live handlers for finished tasks.
      if (ev.type === 'done' || ev.type === 'error' || ev.type === 'aborted') {
        if (ev.type === 'done') {
          s.status = 'done';
          if (ev.report) s.fullText = ev.report;
        } else if (ev.type === 'aborted') {
          s.status = 'aborted';
          if (typeof ev.partial === 'string' && ev.partial) {
            s.fullText = ev.partial;
            s.contentStarted = true;
          }
        } else {
          s.status = 'error';
        }
        _detachReportPush(s);
        // Let the poll do the authoritative terminal fetch (report body,
        // meta, resolvedTitle, DB persistence side-effects). The push frame
        // stops the spinner NOW; the poll fills in the rest.
        if (typeof runtimeScope._pollReportTask === 'function') runtimeScope._pollReportTask(view);
        dirty = true;
      }
      if (dirty && s.paperId === runtimeScope._activePaperId) _paintReportFromState(view);
    });
    s._pushTaskId = taskId;
  } catch (e) {
    // A failed subscription is NOT fatal: the poll floor still converges.
    console.debug('[Paper:Report] push subscribe failed:', e);
  }
}

/** Release a report stream's push subscription. */
function _detachReportPush(s) {
  if (!s || !s._pushTaskId) return;
  try {
    if (typeof pushUnsubscribe === 'function') pushUnsubscribe('paper', s._pushTaskId);
  } catch (e) {
    console.debug('[Paper:Report] push unsubscribe failed:', e);
  }
  s._pushTaskId = '';
}


/** Ordered, exactly-once ingest gate for report events ().
 *
 * ── Why this exists ──
 * The report view now has TWO transports feeding the same state:
 *   • the 'paper' push channel — frames arrive the instant the backend appends
 *     them (report_runtime sets push_channel='paper'), and
 *   • the 1.2s poll — the catch-up floor for a client whose WebSocket is
 *     blocked by a proxy, and the reconnect path after a refresh.
 * Both deliver the SAME events, so applying them naively would double-append
 * every delta (the report body rendered twice) and re-push tool rounds.
 *
 * Every event carries a monotonic ``seq`` (assigned in TaskRuntime.append_event),
 * which makes de-duplication exact rather than heuristic: apply an event only
 * when its seq advances the stream's high-water mark. That also keeps the two
 * transports ORDERED with respect to each other — a push frame that overtakes
 * the poll is applied once, and the poll's later replay of it is a no-op.
 *
 * An event with no seq (defensive: an older server, or a synthetic frame) is
 * applied unconditionally — dropping it would be worse than a rare duplicate.
 */
function _applyReportEvent(s, ev) {
  if (!s || !ev) return false;
  var seq = ev.seq;
  if (typeof seq === 'number') {
    if (s._seqSeen == null) s._seqSeen = -1;
    if (seq <= s._seqSeen) return false;     // already applied by the other transport
    s._seqSeen = seq;
  }
  return _applyReportEventRaw(s, ev);
}

/* ── Versioned tool_progress replay safety (tofu.tool-progress/v1) ───────
 * ``tool_progress`` carries a monotonic per-call ``seq`` (1-based) alongside
 * the ordered ``chunk`` delta. The task-level seq gate in _applyReportEvent
 * already dedupes whole events across the push/poll transports; this gate is
 * the per-call defense — it rejects a duplicate or out-of-order chunk for the
 * same round so the bounded partial-output buffer never double-appends. A
 * legacy frame without seq still applies unconditionally. */
var _REPORT_TOOL_PROGRESS_BOUNDED_CHARS = 100000;
var _REPORT_TOOL_PROGRESS_TRUNCATION_MARKER =
  '\n\n… [live output truncated] …\n\n';
var _REPORT_TOOL_PROGRESS_TRUNCATION_TAIL_RE =
  /… \[live output truncated[^\]]*\] …\n\n([\s\S]*)$/;

function _reportProgressTail(value) {
  var existing = typeof value === 'string' ? value : '';
  var match = _REPORT_TOOL_PROGRESS_TRUNCATION_TAIL_RE.exec(existing);
  return match ? match[1] : existing;
}

function _reportProgressStatus(truncated, spooling, reason) {
  if (reason === 'cancelling') return 'cancelling';
  if (reason === 'cancelled' || reason === 'cancelled-partial') {
    return 'cancelled-partial';
  }
  if (truncated) return 'overflow-loss';
  if (spooling) return 'spooling';
  return 'running';
}

function _reportApplyToolProgress(round, ev) {
  var seq = typeof ev.seq === 'number' ? ev.seq : null;
  var lastSeq = typeof round._partialOutputSeq === 'number'
    ? round._partialOutputSeq : 0;
  if (seq !== null && seq <= lastSeq) return false;

  var chunk = typeof ev.chunk === 'string' ? ev.chunk : '';
  var existingTotal = typeof round._partialOutputTotalChars === 'number'
    ? round._partialOutputTotalChars
    : _reportProgressTail(round._partialOutput).length;
  var totalChars = existingTotal + chunk.length;
  var truncated = round._partialOutputTruncated === true
    || ev.truncated === true
    || totalChars > _REPORT_TOOL_PROGRESS_BOUNDED_CHARS;
  var spooling = ev.spooling === true;
  var reason = typeof ev.terminalReason === 'string' ? ev.terminalReason : '';

  if (!truncated) {
    round._partialOutput = (typeof round._partialOutput === 'string'
      ? round._partialOutput : '') + chunk;
  } else {
    var suffixLimit = _REPORT_TOOL_PROGRESS_BOUNDED_CHARS
      - Math.floor(_REPORT_TOOL_PROGRESS_BOUNDED_CHARS * 3 / 4);
    var tail = (_reportProgressTail(round._partialOutput) + chunk)
      .slice(-suffixLimit);
    round._partialOutput = _REPORT_TOOL_PROGRESS_TRUNCATION_MARKER + tail;
  }
  round._partialOutputTotalChars = totalChars;
  round._partialOutputTruncated = truncated;
  round._partialOutputStatus = _reportProgressStatus(truncated, spooling, reason);
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
function _applyReportEventRaw(s, ev) {
  switch (ev.type) {
    case 'status':
      s.status = ev.status || s.status;
      return true;

    case 'thinking':
      s.thinkingText += (ev.delta || '');
      _segAppendProse(s, 'thinking', ev.delta || '', _segRoundOf(s, ev));
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
      _segApplyToolStart(s, ev);
      return true;
    }

    case 'tool_done': {
      var r = null;
      for (var i = 0; i < s.toolRounds.length; i++) {
        if (s.toolRounds[i].roundNum === ev.roundNum) { r = s.toolRounds[i]; break; }
      }
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
      var rp = null;
      for (var j = 0; j < s.toolRounds.length; j++) {
        if (s.toolRounds[j].roundNum === ev.roundNum) { rp = s.toolRounds[j]; break; }
      }
      if (rp) {
        _reportApplyToolProgress(rp, ev);
      }
      return true;
    }

    case 'delta':
      s.fullText += (ev.delta || '');
      s.contentStarted = true;
      _segAppendProse(s, 'text', ev.delta || '', _segRoundOf(s, ev));
      return true;

    case 'delta_reset':
      // The model emitted an interim draft alongside a tool call; the
      // backend discards it and will rewrite the full report after the tool
      // results land. Clear the accumulated text so the draft + final report
      // don't concatenate (report rendered twice).
      s.fullText = '';
      s.contentStarted = false;
      s._lastRenderedLen = -1;
      // Resolve through the explicit window seam so native owners and
      // fault-injection tests can replace the reducer without relying on
      // bundler-specific top-level-name exposure.
      (runtimeScope._segApplyDeltaReset || _segApplyDeltaReset)(s, ev);
      return true;

    case 'enriched':
      s.fullText = ev.text || s.fullText;
      // Only mutate global hash when this stream still belongs to the active paper —
      // a stream that was started for paper A and is now polling in the background
      // must not stomp paper B's hash.
      if (ev.paperHash && s.paperId === runtimeScope._activePaperId) _paperHash = ev.paperHash;
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
      if (typeof runtimeScope._paperXpHandleInsightEvent === 'function'
          && runtimeScope._paperXpHandleInsightEvent(s, ev, _reportView(s.kind))) {
        return true;
      }
      // The insight pass produced a grounded synthesis/transfer section. It is
      // a self-contained Markdown block (## 💡 …) persisted separately under
      // the `insight:<ui>` key; render it live by appending to the report body
      // (and the cached snapshot) so it flows through the same markdown
      // renderer + TOC. Guard against double-append on cursor replay.
      s._insightRunning = false;
      var _ins = ev.insight || '';
      if (_ins && !s._insightApplied) {
        s._insightApplied = true;
        s.insightText = _ins;
        s.fullText = (s.fullText || '').replace(/\s*$/, '') + '\n\n' + _ins + '\n';
        s._lastRenderedLen = -1;   // force re-render
        if (s.paperId === runtimeScope._activePaperId) {
          var _vIns = _reportView(s.kind);
          if (_vIns.cache) {
            _vIns.cache = _vIns.cache.replace(/\s*$/, '') + '\n\n' + _ins + '\n';
            runtimeScope._rememberReportSnapshot(_vIns, _vIns.cache, _vIns.meta || s.meta);
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
      if (typeof runtimeScope._paperXpHandleCheckpointsEvent === 'function'
          && runtimeScope._paperXpHandleCheckpointsEvent(s, ev, _reportView(s.kind))) {
        return true;
      }
      return true;

    case 'checkpoints_skipped':
      return true;

    case 'report_meta':
      // Second-pass cost landed after `done` (design §3.3) — the reading-xp
      // rail swaps the finish tag for one carrying the secondPasses breakdown.
      if (typeof runtimeScope._paperXpApplyMetaEvent === 'function'
          && runtimeScope._paperXpApplyMetaEvent(s, ev, _reportView(s.kind))) {
        return true;
      }
      if (ev.meta) {
        s.meta = ev.meta;
        var _vRm = _reportView(s.kind);
        if (s.paperId === runtimeScope._activePaperId) _vRm.meta = ev.meta;
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
      var _add = ev.addendum || '';
      if (_add && !s._termfillApplied) {
        s._termfillApplied = true;
        s.termfillText = _add;
        s.fullText = (s.fullText || '').replace(/\s*$/, '') + '\n\n' + _add + '\n';
        if (s.meta && s.meta.terminologyAudit) s.meta.terminologyAudit = null;
        s._lastRenderedLen = -1;   // force re-render (drops the warning card)
        if (s.paperId === runtimeScope._activePaperId) {
          var _vTf = _reportView(s.kind);
          if (_vTf.meta && _vTf.meta.terminologyAudit) _vTf.meta.terminologyAudit = null;
          if (_vTf.cache) {
            _vTf.cache = _vTf.cache.replace(/\s*$/, '') + '\n\n' + _add + '\n';
            runtimeScope._rememberReportSnapshot(_vTf, _vTf.cache, _vTf.meta || s.meta);
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
      var _vDone = _reportView(s.kind);
      if (ev.report) {
        s.fullText = ev.report;
        if (s.paperId === runtimeScope._activePaperId) {
          _vDone.cache = ev.report;
          runtimeScope._rememberReportSnapshot(_vDone, ev.report, ev.meta || s.meta);
          _persistGeneratedReviewVenue(_vDone, _vDone.langKey(), s.paperId);
        }
      }
      if (ev.meta) {
        s.meta = ev.meta;
        if (s.paperId === runtimeScope._activePaperId) _vDone.meta = ev.meta;
      }
      if (ev.paperHash && s.paperId === runtimeScope._activePaperId) _paperHash = ev.paperHash;
      if (ev.resolvedTitle) _applyResolvedTitle(ev.resolvedTitle, s.paperId);
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
      s._errorEnv = (typeof normalizeErrorEnvelope === 'function')
        ? normalizeErrorEnvelope(ev.error)
        : null;
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(ev.error) : '')
                || (typeof ev.error === 'string' ? ev.error : '')
                || 'Unknown error';
      return true;
  }
  return false;
}

/* ── Report render-layer enhancement ──────────────────────────────────
 * The report is plain Markdown rendered by renderMarkdown(). To make the
 * finished report richer WITHOUT moving layout responsibility onto the model
 * (which would break streaming, theming, caching and safety), we post-process
 * the rendered DOM: heading anchors, a sticky TOC sidebar, styled callout
 * boxes (blockquotes that open with a keyword) and framed figures.
 * Intermediate streaming frames stay as plain renderMarkdown() — enhancement
 * only runs on the final / cached render. */

// Order matters: multi-char / more-specific keywords (takeaway) are tested
// before the broad ones (important's bare "关键") so "关键结论：" classifies as
// a takeaway, not important. The trailing (?:[:：]|\b) accepts a colon (the
// form the prompt asks for, and the only thing that works after CJK since \b
// does not fire between two CJK chars) OR an ASCII word boundary (English
// keywords without a colon).
var _REPORT_CALLOUT_KEYWORDS = [
  { cls: 'takeaway', re: /^(key takeaway|takeaway|key point|key finding|summary|bottom line|关键结论|核心结论|要点|总结|小结)(?:[:：]|\b)/i },
  { cls: 'warning', re: /^(warning|caution|caveat|limitation|警告|注意|局限|风险)(?:[:：]|\b)/i },
  { cls: 'important', re: /^(important|critical|重要|关键)(?:[:：]|\b)/i },
  { cls: 'tip', re: /^(tip|pro tip|提示|建议)(?:[:：]|\b)/i },
  { cls: 'note', re: /^(note|nb|备注|说明)(?:[:：]|\b)/i },
];

function _slugifyHeading(text, used) {
  var base = String(text || '')
    .toLowerCase().trim()
    .replace(/[^\w\u4e00-\u9fff\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '') || 'section';
  var slug = base, n = 2;
  while (used[slug]) { slug = base + '-' + n; n++; }
  used[slug] = true;
  return slug;
}

/** Decorate blockquotes that open with a keyword into themed callout boxes. */
function _decorateCallouts(article) {
  var quotes = article.querySelectorAll('blockquote');
  for (var i = 0; i < quotes.length; i++) {
    var bq = quotes[i];
    if (bq.closest('.paper-callout')) continue;
    var lead = (bq.textContent || '').trimStart();
    var match = null;
    for (var k = 0; k < _REPORT_CALLOUT_KEYWORDS.length; k++) {
      if (_REPORT_CALLOUT_KEYWORDS[k].re.test(lead)) { match = _REPORT_CALLOUT_KEYWORDS[k]; break; }
    }
    if (!match) continue;
    bq.classList.add('paper-callout', 'paper-callout-' + match.cls);
  }
}

/** Wrap image-only paragraphs into <figure> with a <figcaption>. */
function _frameFigures(article) {
  var imgs = article.querySelectorAll('img');
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    if (img.closest('figure')) continue;
    var p = img.closest('p');
    if (!p) continue;
    // Only wrap when the paragraph is essentially just the image (+ caption em)
    var hasOtherText = (p.textContent || '').trim().length > 0
      && !p.querySelector('em') && !img.getAttribute('alt');
    if (hasOtherText) continue;
    var fig = document.createElement('figure');
    fig.className = 'paper-figure';
    fig.appendChild(img.cloneNode(true));
    var capText = '';
    var em = p.querySelector('em');
    if (em && em.textContent.trim()) capText = em.textContent.trim();
    else if (img.getAttribute('alt')) capText = img.getAttribute('alt').trim();
    if (capText) {
      var cap = document.createElement('figcaption');
      cap.textContent = capText;
      fig.appendChild(cap);
    }
    p.parentNode.replaceChild(fig, p);
  }
}

/* ── Glossary hover-definitions ───────────────────────────────────────
 * The report opens with a "Core Terminology" table. A reader deep in the
 * Method or Experiments section has long forgotten those definitions, so the
 * report stops being self-contained exactly where it matters most. We parse
 * that table once, then turn LATER mentions of each term into a subtly
 * underlined span whose definition appears on hover/focus — no scrolling back.
 * To keep it from becoming visual noise we decorate each term at most once
 * per top-level (h2) section. */

/** Plain-text of a rendered table cell WITHOUT KaTeX's duplicated LaTeX
 *  source. KaTeX emits both a visual .katex-html tree and a hidden
 *  .katex-mathml annotation that carries the raw TeX; a naive textContent
 *  concatenates both (→ "T/F=生成长度/NFE\text{T/F}=…"). We clone the node,
 *  drop the mathml duplicates, then read textContent. */
function _cellPlainText(cell) {
  if (!cell) return '';
  var txt;
  try {
    var clone = cell.cloneNode(true);
    var dup = clone.querySelectorAll('.katex-mathml, annotation');
    for (var i = 0; i < dup.length; i++) {
      if (dup[i].parentNode) dup[i].parentNode.removeChild(dup[i]);
    }
    txt = clone.textContent || '';
  } catch (e) {
    txt = cell.textContent || '';
  }
  return txt.replace(/\s+/g, ' ').trim();
}

/** Find the "Core Terminology / 核心术语" table, tag it, and return its rows
 *  as [{term, def, defHtml}]. Returns [] when the report has no such table. */
function _extractGlossary(article) {
  var tables = article.querySelectorAll('table');
  for (var ti = 0; ti < tables.length; ti++) {
    var table = tables[ti];
    var head = table.querySelector('thead th, tr:first-child th');
    var first = (head && head.textContent || '').trim().toLowerCase();
    // The prompt fixes the first column header to "Term" (EN) / "术语" (ZH).
    if (first !== 'term' && first.indexOf('术语') < 0) continue;
    table.classList.add('paper-glossary');
    var rows = table.querySelectorAll('tbody tr');
    var out = [];
    for (var ri = 0; ri < rows.length; ri++) {
      var cells = rows[ri].querySelectorAll('td');
      if (cells.length < 2) continue;
      var term = (cells[0].textContent || '').trim();
      // The definition cell is part of the already-rendered report, so it may
      // hold live KaTeX / <strong> / <code> DOM. Capture its HTML so the hover
      // card can render it, and derive a CLEAN plain-text form for aria-label:
      // a naive textContent would garble math because KaTeX duplicates its
      // LaTeX source into a hidden .katex-mathml annotation node.
      var defCell = cells[1];
      var defHtml = (defCell.innerHTML || '').trim();
      var defText = _cellPlainText(defCell);
      if (!term || !defText) continue;
      // Skip the prompt's own placeholder rows: "(term)", "（术语）", "...".
      if (/^[(（].*[)）]$/.test(term) || term === '...' || term === '…') continue;
      if (defText.length > 260) defText = defText.slice(0, 257) + '…';
      // Optional 4th column (reading-xp P2): the everyday analogy. Tolerated
      // by every parser when absent (3-column legacy reports render as before).
      var analogy = '';
      if (cells.length >= 4) {
        analogy = _cellPlainText(cells[3]);
        if (analogy === '—' || analogy === '-' || analogy === '–') analogy = '';
      }
      out.push({ term: term, def: defText, defHtml: defHtml, analogy: analogy });
    }
    return out;  // only the first matching table
  }
  return [];
}

/** Expand a glossary term cell into matchable aliases, e.g.
 *  "Test-Time Scaling (TTS)" → ["Test-Time Scaling", "TTS"],
 *  "Best@K / Oracle Pass@K / Random@K" → [3 variants],
 *  "Agentic Rubrics（本文首创）" → ["Agentic Rubrics"] (meta note dropped). */
function _glossaryAliases(term) {
  var raw = [];
  raw.push(term);
  var base = term.replace(/[(（][^)）]*[)）]/g, '').trim();   // strip parentheticals
  raw.push(base);
  var paren = term.match(/[(（]([^)）]+)[)）]/);
  if (paren) {
    var inner = paren[1].trim();
    // Drop meta annotations the prompt may add; keep real abbreviations.
    if (!/本文首创|首创|借鉴|新增|高\/低|效用|introduced|borrowed|coined/i.test(inner)) raw.push(inner);
  }
  base.split(/\s*[\/、，]\s*/).forEach(function (p) { raw.push(p); });

  var seen = {}, out = [];
  for (var i = 0; i < raw.length; i++) {
    var a = (raw[i] || '').trim();
    if (!a) continue;
    var key = a.toLowerCase();
    if (seen[key]) continue;
    var hasCjk = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(a);
    var isAbbrev = /^[A-Z0-9][A-Z0-9@+\-]{1,}$/.test(a);   // e.g. TTS, RL, Best@K
    // Length gate: CJK ≥2 chars, Latin ≥3 chars (abbreviations ≥2).
    if (hasCjk) { if (a.length < 2) continue; }
    else if (!isAbbrev && a.length < 3) continue;
    seen[key] = true;
    out.push(a);
  }
  return out;
}

function _escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** Decorate later mentions of glossary terms with hover-definition spans. */
function _decorateGlossaryTerms(article, glossary) {
  if (!glossary || !glossary.length || typeof document === 'undefined') return;

  // Build alias → {row, def} map and a combined matcher (longest alias first
  // so "Oracle Pass@K" wins over a bare "Oracle").
  var map = {}, aliases = [];
  for (var r = 0; r < glossary.length; r++) {
    var al = _glossaryAliases(glossary[r].term);
    for (var j = 0; j < al.length; j++) {
      var key = al[j].toLowerCase();
      if (map[key]) continue;       // first row to claim an alias keeps it
      map[key] = { row: r, def: glossary[r].def, defHtml: glossary[r].defHtml,
                   analogy: glossary[r].analogy || '' };
      aliases.push(al[j]);
    }
  }
  if (!aliases.length) return;
  aliases.sort(function (a, b) { return b.length - a.length; });
  var re;
  try {
    re = new RegExp(aliases.map(_escapeRegExp).join('|'), 'gi');
  } catch (e) {
    console.warn('[Paper:Glossary] regex build failed:', e);
    return;
  }

  var SKIP = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, CODE: 1, PRE: 1,
               A: 1, FIGCAPTION: 1, SCRIPT: 1, STYLE: 1, BUTTON: 1 };
  var seen = {};   // row → already decorated in the current section

  function decorateText(node) {
    var text = node.nodeValue;
    if (!text || text.length < 2 || !/\S/.test(text)) return;
    re.lastIndex = 0;
    var picks = [], m, pos = 0;
    while ((m = re.exec(text))) {
      var matched = m[0], idx = m.index, key = matched.toLowerCase();
      var entry = map[key];
      if (re.lastIndex === idx) re.lastIndex++;   // zero-width safety
      if (!entry || seen[entry.row]) continue;
      // Latin word-boundary guard (avoid matching inside a larger word).
      var headLatin = /[A-Za-z0-9]/.test(matched.charAt(0));
      var tailLatin = /[A-Za-z0-9]/.test(matched.charAt(matched.length - 1));
      if (headLatin && idx > 0 && /[A-Za-z0-9]/.test(text.charAt(idx - 1))) continue;
      if (tailLatin && /[A-Za-z0-9]/.test(text.charAt(idx + matched.length))) continue;
      if (idx < pos) continue;       // overlaps a prior pick
      picks.push({ idx: idx, len: matched.length, text: matched, def: entry.def,
                   defHtml: entry.defHtml, analogy: entry.analogy });
      seen[entry.row] = true;
      pos = idx + matched.length;
    }
    if (!picks.length) return;
    var frag = document.createDocumentFragment(), cursor = 0;
    for (var p = 0; p < picks.length; p++) {
      var pk = picks[p];
      if (pk.idx > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, pk.idx)));
      var span = document.createElement('span');
      span.className = 'paper-term';
      span.setAttribute('tabindex', '0');
      span.setAttribute('aria-label', pk.text + ': ' + pk.def);
      span.appendChild(document.createTextNode(pk.text));
      // Real DOM hover card so the definition renders its Markdown/KaTeX
      // (a CSS attr(data-def) ::after can only show plain text). The HTML
      // came from the already-sanitized report body, so it's safe to reuse.
      var card = document.createElement('span');
      card.className = 'paper-term-card';
      card.setAttribute('aria-hidden', 'true');   // aria-label on the span already carries the def
      // Analogy first (reading-xp P2): the reader remembers the comparison
      // before the definition — it heads the hover card when present.
      if (pk.analogy) {
        var an = document.createElement('span');
        an.className = 'paper-term-card-analogy';
        an.textContent = '💡 ' + pk.analogy;
        card.appendChild(an);
      }
      var defBody = document.createElement('span');
      defBody.className = 'paper-term-card-def';
      if (pk.defHtml) defBody.innerHTML = pk.defHtml;
      else defBody.textContent = pk.def;
      card.appendChild(defBody);
      span.appendChild(card);
      frag.appendChild(span);
      cursor = pk.idx + pk.len;
    }
    if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
    node.parentNode.replaceChild(frag, node);
  }

  function walk(node) {
    var children = Array.prototype.slice.call(node.childNodes);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.nodeType === 3) { decorateText(c); continue; }
      if (c.nodeType !== 1) continue;
      var tag = c.tagName;
      if (tag === 'H1' || tag === 'H2') seen = {};   // new section → re-allow terms
      if (SKIP[tag]) continue;
      if (c.classList && (c.classList.contains('paper-glossary') ||
          c.classList.contains('paper-term') || c.classList.contains('katex'))) continue;
      walk(c);
    }
  }

  try { walk(article); }
  catch (e) { console.warn('[Paper:Glossary] decoration failed:', e); }
}

/** Assign stable ids to h2/h3 and return the TOC entry list. */
function _indexHeadings(article) {
  var heads = article.querySelectorAll('h2, h3');
  var used = {}, entries = [];
  for (var i = 0; i < heads.length; i++) {
    var h = heads[i];
    var text = (h.textContent || '').trim();
    if (!text) continue;
    if (!h.id) h.id = 'report-' + _slugifyHeading(text, used);
    entries.push({ id: h.id, text: text, level: h.tagName === 'H3' ? 3 : 2 });
  }
  return entries;
}

function _buildReportTOC(entries) {
  if (entries.length < 3) return '';  // not worth a sidebar for a tiny report
  var label = t('paper.reportTocLabel');
  var html = '<nav class="paper-report-toc" aria-label="' + label + '">'
    + '<div class="paper-report-toc-title">' + label + '</div><ul>';
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    html += '<li class="toc-l' + e.level + '"><a href="#' + e.id + '" data-target="' + e.id
      + '" data-tofu-action="_scrollReportToHeading(event,\'' + e.id + '\')">' + escapeHtml(e.text) + '</a></li>';
  }
  html += '</ul></nav>';
  return html;
}

function _scrollReportToHeading(ev, id) {
  if (ev) ev.preventDefault();
  var el = document.getElementById(id);
  if (!el) return;
  // Scroll ONLY the report's own scroll container. el.scrollIntoView() would
  // scroll every scrollable ancestor — including the outer overflow:hidden
  // containers (.paper-tab-panel / .paper-body / .paper-mode-container), which
  // are still programmatically scrollable. That pushes the .paper-tabs bar out
  // of view with no scrollbar to bring it back. Scroll the inner container
  // manually so the chrome above the report never moves.
  var _rm = (typeof prefersReducedMotion === 'function') && prefersReducedMotion();
  /** @type {ScrollBehavior} */
  var _behavior = _rm ? 'auto' : 'smooth';
  var scroller = el.closest('.paper-report-content, .paper-report-body');
  if (!scroller) { el.scrollIntoView({ behavior: _behavior, block: 'start' }); return; }
  var TOP_MARGIN = 16;  // matches h2/h3 scroll-margin-top
  var target = scroller.scrollTop
    + (el.getBoundingClientRect().top - scroller.getBoundingClientRect().top) - TOP_MARGIN;
  scroller.scrollTo({ top: Math.max(0, target), behavior: _behavior });
}

/** Scroll-spy: highlight the TOC entry for the heading currently in view. */
function _wireReportScrollSpy(scrollEl, article, toc) {
  if (!scrollEl || !toc || typeof IntersectionObserver === 'undefined') return;
  var links = {};
  toc.querySelectorAll('a[data-target]').forEach(function(a) { links[a.getAttribute('data-target')] = a; });
  var heads = article.querySelectorAll('h2, h3');
  if (!heads.length) return;
  var visible = {};
  var obs = new IntersectionObserver(function(items) {
    items.forEach(function(it) { visible[it.target.id] = it.isIntersecting; });
    var firstActive = null;
    for (var i = 0; i < heads.length; i++) { if (visible[heads[i].id]) { firstActive = heads[i].id; break; } }
    Object.keys(links).forEach(function(k) { links[k].classList.toggle('active', k === firstActive); });
  }, { root: scrollEl, rootMargin: '0px 0px -70% 0px', threshold: 0 });
  for (var i = 0; i < heads.length; i++) obs.observe(heads[i]);
  // Stash so a later re-render can disconnect the stale observer.
  if (scrollEl._reportSpyObs) { try { scrollEl._reportSpyObs.disconnect(); } catch (e) {} }
  scrollEl._reportSpyObs = obs;
}

/** Build the rebuttal score-decision card. Rendered at the top of a rebuttal
 *  reply from the server-parsed decision the model emitted below its sentinel.
 *  Highlights the OA/Confidence transition (e.g. "OA 4 → 5") and, crucially,
 *  makes an UNCHANGED decision visually first-class (most rebuttals should not
 *  move a score). `d` = {origOverall,newOverall,origConfidence,newConfidence,
 *  overallChanged,confidenceChanged,changed,reason}. Returns '' when absent. */
function _renderRebuttalDecisionCard(d) {
  if (!d || !d.present) return '';
  function esc(v) { return escapeHtml(String(v == null ? '' : v)); }
  function arrow(changed) {
    // ▲ up / ▼ down / — same, decided per-dimension by numeric comparison
    // where possible (scales may be non-numeric like "Weak Accept").
    return changed ? '\u2192' : '';
  }
  function pair(label, from, to, changed) {
    var fromS = esc(from), toS = esc(to);
    if (!fromS && !toS) return '';
    var body = changed
      ? '<span class="rd-from">' + fromS + '</span> <span class="rd-arrow">\u2192</span> <span class="rd-to">' + toS + '</span>'
      : '<span class="rd-same">' + (toS || fromS) + '</span>';
    return '<div class="rd-metric' + (changed ? ' rd-changed' : '') + '">' +
      '<span class="rd-label">' + esc(label) + '</span>' + body + '</div>';
  }
  var changed = !!d.changed;
  var oaLabel = (typeof t === 'function') ? t('paper.rebuttalOverall') : 'Overall';
  var confLabel = (typeof t === 'function') ? t('paper.rebuttalConfidence') : 'Confidence';
  var verdict = changed
    ? ((typeof t === 'function') ? t('paper.rebuttalScoreChanged') : 'Score adjusted')
    : ((typeof t === 'function') ? t('paper.rebuttalScoreUnchanged') : 'Score unchanged');
  var reasonHtml = d.reason
    ? '<div class="rd-reason">' + esc(d.reason) + '</div>' : '';
  return '<div class="paper-rebuttal-decision ' + (changed ? 'rd-yes' : 'rd-no') + '">' +
    '<div class="rd-head">' +
      '<span class="rd-badge">' + esc(verdict) + '</span>' +
    '</div>' +
    '<div class="rd-metrics">' +
      pair(oaLabel, d.origOverall, d.newOverall, !!d.overallChanged) +
      pair(confLabel, d.origConfidence, d.newConfidence, !!d.confidenceChanged) +
    '</div>' +
    reasonHtml +
  '</div>';
}

/** Build the citation-integrity card. Rendered ONLY when the server attached a
 *  `citationAudit` to the report meta — which it does ONLY when at least one
 *  cited identifier is suspicious. `unverifiable` entries are never surfaced
 *  here (they are coverage gaps, not hallucinations). Returns '' when absent.
 *  `audit` = {total, counts:{verified,suspicious,unverifiable}, suspicious:[…]} */
function _renderCitationAuditCard(audit) {
  if (!audit || !audit.suspicious || !audit.suspicious.length) return '';
  var c = audit.counts || {};
  var n = audit.suspicious.length;
  var title = t('paper.citeAuditTitle', { n: n });
  var sub = t('paper.citeAuditSub', {
    total: (audit.total || 0), verified: (c.verified || 0),
    suspicious: (c.suspicious || 0), unverifiable: (c.unverifiable || 0)
  });
  var rows = audit.suspicious.map(function (it) {
    var idLabel = escapeHtml((it.kind || '') + ' ' + (it.identifier || ''));
    var reason = escapeHtml(it.reason || t('paper.citeDidNotResolve'));
    var checked = it.checked
      ? ('<a class="paper-cite-checked" href="' + escapeHtml(it.checked) +
         '" target="_blank" rel="noopener noreferrer">' +
         escapeHtml(t('paper.citeCheckedSource')) + '</a>')
      : '';
    var titles = '';
    if (it.matchedTitle && it.claimedTitle) {
      titles = '<div class="paper-cite-titles">' +
        '<span class="paper-cite-claimed">' + escapeHtml(t('paper.citeClaimed')) +
        escapeHtml(it.claimedTitle) + '</span>' +
        '<span class="paper-cite-matched">' + escapeHtml(t('paper.citeResolvesTo')) +
        escapeHtml(it.matchedTitle) + '</span></div>';
    }
    return '<li class="paper-cite-item">' +
      '<code class="paper-cite-id">' + idLabel + '</code>' +
      '<span class="paper-cite-reason">' + reason + '</span>' +
      checked + titles + '</li>';
  }).join('');
  return '<aside class="paper-citation-audit" role="alert">' +
    '<div class="paper-cite-head">' +
      '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
      '<span class="paper-cite-title">' + escapeHtml(title) + '</span>' +
    '</div>' +
    '<p class="paper-cite-sub">' + escapeHtml(sub) + '</p>' +
    '<ul class="paper-cite-list">' + rows + '</ul>' +
    '</aside>';
}

/** Build the terminology self-containment card. Rendered ONLY when the server
 *  attached a `terminologyAudit` to the report meta — which it does ONLY when
 *  the glossary has a real gap (a term used in the body with no glossary row,
 *  or a glossary definition leaning on an undefined sibling term). Returns ''
 *  when absent or empty. `audit` = {glossaryCount, counts:{missing,dangling},
 *  missing:[{term,section,evidence}], dangling:[{term,referencedTerm,definition}]}. */
function _renderTerminologyAuditCard(audit) {
  if (!audit) return '';
  var missing = (audit.missing || []);
  var dangling = (audit.dangling || []);
  if (!missing.length && !dangling.length) return '';
  var total = missing.length + dangling.length;
  var title = t('paper.termAuditTitle', { n: total });
  var sub = t('paper.termAuditSub', {
    glossaryCount: (audit.glossaryCount || 0),
    missing: missing.length, dangling: dangling.length
  });
  var items = [];
  missing.forEach(function (m) {
    var where = m.section
      ? ('<span class="paper-term-where">' +
         escapeHtml(t('paper.termAppearsIn') + m.section) + '</span>')
      : '';
    var ev = m.evidence
      ? ('<span class="paper-term-evidence">' + escapeHtml(m.evidence) + '</span>')
      : '';
    items.push('<li class="paper-term-item paper-term-missing">' +
      '<code class="paper-term-id">' + escapeHtml(m.term || '') + '</code>' +
      '<span class="paper-term-reason">' +
        escapeHtml(t('paper.termUsedNotInGlossary')) + '</span>' +
      where + ev + '</li>');
  });
  dangling.forEach(function (d) {
    var reason = t('paper.termDanglingReason', { term: (d.term || ''), referencedTerm: (d.referencedTerm || '') });
    items.push('<li class="paper-term-item paper-term-dangling">' +
      '<code class="paper-term-id">' + escapeHtml(d.referencedTerm || '') + '</code>' +
      '<span class="paper-term-reason">' + escapeHtml(reason) + '</span></li>');
  });
  return '<aside class="paper-terminology-audit" role="note">' +
    '<div class="paper-cite-head">' +
      '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
      '<span class="paper-cite-title">' + escapeHtml(title) + '</span>' +
    '</div>' +
    '<p class="paper-cite-sub">' + escapeHtml(sub) + '</p>' +
    '<ul class="paper-cite-list">' + items.join('') + '</ul>' +
    '</aside>';
}

/** Build the "finish tag" badge: which model generated the report + its cost.
 *  Visually subtle, sits at the END of the report so it never disrupts content.
 *  `meta` is the server-supplied dict ({model, costCny, costUsd, promptTokens,
 *  completionTokens, rounds, elapsedSec}). Returns '' when meta is absent. */
function _renderReportFinishTag(meta) {
  if (!meta || !meta.model) return '';
  var parts = [];
  // Model — the headline of the tag.
  parts.push('<span class="paper-finish-model" title="' +
    escapeHtml(t('paper.finishModelTitle')) + '">' +
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0 0 6 3 3 0 0 0 3 3v1a3 3 0 0 0 6 0v-1a3 3 0 0 0 3-3 3 3 0 0 0 0-6 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/></svg>' +
    escapeHtml(meta.model) + '</span>');
  // Cost — prefer CNY (matches the rest of the app), fall back to USD.
  // Cost visibility (design §3.3): when second passes billed extra, the
  // headline figure is the TOTAL (body + passes) and the tooltip breaks it
  // down per pass; with no passes the tag is byte-identical to before.
  var costStr = '';
  var costTitle = t('paper.finishCostTitle');
  var _hasPasses = meta.secondPasses && typeof meta.totalCostCny === 'number'
    && meta.totalCostCny > 0;
  var _effCny = _hasPasses ? meta.totalCostCny : meta.costCny;
  var _effUsd = _hasPasses ? meta.totalCostUsd : meta.costUsd;
  if (typeof _effCny === 'number' && _effCny > 0) {
    costStr = (typeof formatCny === 'function') ? formatCny(_effCny)
      : ('¥' + _effCny.toFixed(4));
  } else if (typeof _effUsd === 'number' && _effUsd > 0) {
    costStr = '$' + _effUsd.toFixed(4);
  }
  if (_hasPasses && typeof runtimeScope._paperXpCostBreakdown === 'function') {
    var _bd = runtimeScope._paperXpCostBreakdown(meta);
    if (_bd) costTitle = _bd;
  }
  if (costStr) {
    parts.push('<span class="paper-finish-cost" title="' +
      escapeHtml(costTitle) +
      '">' + escapeHtml(costStr) + '</span>');
  }
  // Tokens (compact) — secondary detail.
  var inTok = meta.promptTokens || 0;
  var outTok = meta.completionTokens || 0;
  if (inTok || outTok) {
    var fmt = function (n) {
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
      if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
      return String(n);
    };
    parts.push('<span class="paper-finish-tokens" title="' +
      escapeHtml(t('paper.finishTokensTitle')) + '">' +
      fmt(inTok) + ' \u2192 ' + fmt(outTok) + ' tok</span>');
  }
  var label = t('paper.finishGeneratedBy');
  return '<div class="paper-report-finish-tag" role="contentinfo">' +
    '<span class="paper-finish-label">' + escapeHtml(label) + '</span>' +
    parts.join('') + '</div>';
}

/* ── Reading-time estimate + progress bar (Report tab) ───────────────────
 *
 * We show, sticky at the top of the rendered report, an estimated reading
 * time + a progress bar that fills as the user scrolls, with a live
 * "remaining time" readout.
 *
 * The estimate uses a LEARNING reading-speed model:
 *   • Cold start  → a sensible default WPM for dense technical prose.
 *   • Over time   → an exponentially-weighted moving average (EWMA) of the
 *     user's OBSERVED speed, measured from real reading sessions (words
 *     covered ÷ active time spent on the report). Persisted in localStorage,
 *     so it improves across papers and survives reloads.
 *
 * "Words" are counted in a script-aware way: CJK characters are counted
 * individually (people read CJK roughly per-character, much slower per
 * "word"), Latin words by whitespace runs. The model stores a single WPM in
 * a normalized Latin-word equivalent, and we convert CJK char counts to that
 * equivalent with a fixed ratio so one EWMA covers mixed-language reports.
 */

var _READ_SPEED_KEY = 'paper_reading_wpm_v1';
var _READ_WPM_DEFAULT = 220;   // dense technical prose, conservative
var _READ_WPM_MIN = 60;        // clamp learned speed to a sane band
var _READ_WPM_MAX = 700;
var _READ_EWMA_ALPHA = 0.25;   // weight of a fresh observation
// One CJK character ≈ this many Latin-word-equivalents (CJK is read slower
// per glyph than an English word, so a char is a fraction of a "word").
var _READ_CJK_CHAR_TO_WORD = 0.6;

// Live tracking state for the currently-displayed report.
var _readTracker = null;

/** Load the learned reading speed (Latin-word WPM). Falls back to default. */
function _loadReadingWpm() {
  try {
    var raw = localStorage.getItem(_READ_SPEED_KEY);
    if (!raw) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
    var o = JSON.parse(raw);
    var wpm = Number(o && o.wpm);
    if (!isFinite(wpm) || wpm <= 0) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
    return { wpm: Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, wpm)),
             samples: (o && o.samples) | 0 };
  } catch (e) {
    console.warn('[Paper:ReadTime] load wpm failed:', e);
    return { wpm: _READ_WPM_DEFAULT, samples: 0 };
  }
}

/** Persist a new observation into the EWMA reading-speed model. */
function _recordReadingObservation(observedWpm) {
  if (!isFinite(observedWpm) || observedWpm <= 0) return;
  observedWpm = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, observedWpm));
  var cur = _loadReadingWpm();
  var next;
  if (cur.samples <= 0) {
    // First-ever real sample: blend gently with the default so one quirky
    // session can't swing the estimate wildly.
    next = _READ_WPM_DEFAULT * 0.5 + observedWpm * 0.5;
  } else {
    next = cur.wpm * (1 - _READ_EWMA_ALPHA) + observedWpm * _READ_EWMA_ALPHA;
  }
  next = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, next));
  try {
    localStorage.setItem(_READ_SPEED_KEY, JSON.stringify({
      wpm: Math.round(next), samples: cur.samples + 1, updatedAt: Date.now(),
    }));
  } catch (e) {
    console.warn('[Paper:ReadTime] persist wpm failed:', e);
  }
}

/** Count reading workload of an element as Latin-word equivalents. */
function _countReadingWords(el) {
  var text = '';
  if (el) {
    // Exclude glossary hover-card text: it's a duplicate of the report body
    // shown only on hover, so counting it would inflate the reading estimate.
    if (el.querySelector && el.querySelector('.paper-term-card')) {
      try {
        var clone = el.cloneNode(true);
        var cards = clone.querySelectorAll('.paper-term-card');
        for (var i = 0; i < cards.length; i++) {
          if (cards[i].parentNode) cards[i].parentNode.removeChild(cards[i]);
        }
        text = clone.textContent || '';
      } catch (e) { text = el.textContent || ''; }
    } else {
      text = el.textContent || '';
    }
  }
  if (!text) return 0;
  // CJK (incl. kana) — counted per character.
  var cjk = (text.match(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g) || []).length;
  // Strip CJK, then count Latin/numeric word runs.
  var latin = text.replace(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g, ' ');
  var latinWords = (latin.match(/[A-Za-z0-9][A-Za-z0-9'\u2019-]*/g) || []).length;
  return latinWords + cjk * _READ_CJK_CHAR_TO_WORD;
}

/** Format a minutes value into a localized human string. */
function _formatReadMinutes(min) {
  var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };
  if (min < 1) return _tt('paper.readTimeLessMin');
  if (min < 60) return _tt('paper.readTimeMin', { n: Math.round(min) });
  var h = Math.floor(min / 60);
  var m = Math.round(min - h * 60);
  return _tt('paper.readTimeHour', { h: h, m: m });
}

/** Build the sticky reading-time header for a freshly rendered report.
 *  `article` is the rendered <article>; `scroller` is the scroll container.
 *  Returns the header element (not yet attached). */
function _buildReadingTimeBar(article, scroller) {
  var words = _countReadingWords(article);
  var model = _loadReadingWpm();
  var totalMin = words / model.wpm;
  var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };

  var bar = document.createElement('div');
  bar.className = 'paper-read-time';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');

  var calib = (model.samples > 0)
    ? _tt('paper.readTimeAdapted', { wpm: Math.round(model.wpm) })
    : _tt('paper.readTimeDefault');

  bar.innerHTML =
    '<div class="paper-read-time-row">' +
      '<span class="paper-read-time-icon" title="' + escapeHtml(calib) + '">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>' +
      '</span>' +
      '<span class="paper-read-time-total"></span>' +
      '<span class="paper-read-time-sep">·</span>' +
      '<span class="paper-read-time-left"></span>' +
      (model.samples > 0 ? '<span class="paper-read-time-badge" title="' + escapeHtml(calib) + '">' +
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>' : '') +
    '</div>' +
    '<div class="paper-read-time-track"><div class="paper-read-time-fill"></div></div>';

  bar._readWords = words;
  bar._readTotalMin = totalMin;
  return bar;
}

/** Wire scroll → progress/remaining updates + the learning tracker.
 *  Disconnects any previous tracker. */
function _wireReadingTimeTracking(bar, scroller, view) {
  if (!bar || !scroller) return;
  // Tear down a previous session (flush its sample first).
  _teardownReadingTracker(true);
  view = view || _reportView('report');

  var totalEl = bar.querySelector('.paper-read-time-total');
  var leftEl = bar.querySelector('.paper-read-time-left');
  var fillEl = bar.querySelector('.paper-read-time-fill');
  var totalMin = bar._readTotalMin || 0;
  var words = bar._readWords || 0;

  if (totalEl) totalEl.textContent =
    (typeof t === 'function' ? t('paper.readTimeTotal', { min: _formatReadMinutes(totalMin) })
                             : _formatReadMinutes(totalMin));

  var tracker = {
    bar: bar, scroller: scroller, words: words, totalMin: totalMin,
    view: view,          // view-context whose reading position we persist
    lastProgress: 0,     // max scroll fraction reached [0..1]
    activeMs: 0,         // accumulated active reading time
    lastTickTs: 0,       // timestamp of last scroll/visibility tick while active
    lastPersistTs: 0,    // throttle: last time we wrote the position to storage
    flushed: false,
    onScroll: null,
    rafPending: false,
  };

  function _progressFraction() {
    var max = scroller.scrollHeight - scroller.clientHeight;
    if (max <= 0) return 1;  // whole report fits — nothing to scroll → fully "covered"
    return Math.max(0, Math.min(1, scroller.scrollTop / max));
  }

  function _paint(frac) {
    if (fillEl) fillEl.style.width = (frac * 100).toFixed(1) + '%';
    bar.setAttribute('aria-valuenow', Math.round(frac * 100));
    var remainMin = totalMin * (1 - frac);
    if (leftEl) {
      if (frac >= 0.999) {
        leftEl.textContent = (typeof t === 'function') ? t('paper.readTimeDone') : 'Finished';
        bar.classList.add('done');
      } else {
        bar.classList.remove('done');
        leftEl.textContent = (typeof t === 'function')
          ? t('paper.readTimeLeft', { min: _formatReadMinutes(remainMin) })
          : _formatReadMinutes(remainMin);
      }
    }
  }

  function _tick() {
    tracker.rafPending = false;
    var now = Date.now();
    var frac = _progressFraction();
    // Accumulate active time only across short gaps between scroll events
    // (a long idle gap = user stepped away / read elsewhere → don't count it).
    if (tracker.lastTickTs && (now - tracker.lastTickTs) < 12000) {
      tracker.activeMs += (now - tracker.lastTickTs);
    }
    tracker.lastTickTs = now;
    if (frac > tracker.lastProgress) tracker.lastProgress = frac;
    _paint(Math.max(frac, 0));
    // Persist the reading position (throttled to ≤ once/second) so a later
    // tab switch / reload restores it. Capture from the live DOM so the anchor
    // is a heading index+offset (survives a cross-language re-layout).
    if (now - tracker.lastPersistTs >= 1000) {
      tracker.lastPersistTs = now;
      runtimeScope._persistReadingPosition(
        tracker.view, runtimeScope._captureReadingAnchor(scroller));
    }
  }

  tracker.onScroll = function() {
    if (tracker.rafPending) return;
    tracker.rafPending = true;
    requestAnimationFrame(_tick);
  };

  scroller.addEventListener('scroll', tracker.onScroll, { passive: true });
  _readTracker = tracker;

  // Initial paint (report may already fit without scrolling).
  _paint(_progressFraction());
}

/** Flush the current reading session into the learning model and detach.
 *  Only records a sample when the session is substantial enough to be a
 *  meaningful signal (enough words covered + enough active time). */
function _teardownReadingTracker(silent) {
  var tk = _readTracker;
  _readTracker = null;
  if (!tk || tk.flushed) return;
  tk.flushed = true;
  try {
    if (tk.scroller && tk.onScroll) tk.scroller.removeEventListener('scroll', tk.onScroll);
  } catch (e) { /* detached node */ }

  // Persist the final reading position before detaching (a tab switch / mode
  // exit flushes here) so re-entry restores exactly where the reader stopped.
  try {
    if (tk.scroller) runtimeScope._persistReadingPosition(
      tk.view, runtimeScope._captureReadingAnchor(tk.scroller));
  } catch (e) { console.debug('[Paper] persist final reading-pos failed: %s', e && e.message); }

  var coveredWords = tk.words * tk.lastProgress;
  var activeMin = tk.activeMs / 60000;
  // Need a real session: covered ≥ ~120 word-equivalents over ≥ 20s of
  // active scrolling. Otherwise it's noise (a glance, an instant scroll-to-end).
  if (coveredWords >= 120 && activeMin >= (20 / 60)) {
    var observedWpm = coveredWords / activeMin;
    _recordReadingObservation(observedWpm);
    if (!silent) {
      console.debug('[Paper:ReadTime] session: %d words / %.2f min → %d wpm',
                    Math.round(coveredWords), activeMin, Math.round(observedWpm));
    }
    // Session wrap-up toast (reading-xp P4) — minutes + coverage + notes.
    if (typeof runtimeScope._paperXpSessionSummary === 'function') {
      try {
        runtimeScope._paperXpSessionSummary({ words: coveredWords, minutes: activeMin }, tk.view);
      } catch (e) {
        console.debug('[Paper] session summary failed (non-fatal): %s', e && e.message);
      }
    }
  }
}

/** Render a FINAL report into `container`: markdown + TOC sidebar + callouts +
 *  framed figures + finish-tag badge. `container` is the scroll element
 *  (.paper-report-content or #reportBodyContent). `meta` (optional) drives the
 *  finish tag; defaults to the module-global `_paperReportMeta`.
 *  Safe to call repeatedly (full rebuild). */
function _renderFinalReport(container, text, meta, view) {
  if (!container) return;
  view = view || _reportView('report');
  if (typeof _syncReportToolbar === 'function') _syncReportToolbar(false, view);
  if (meta === undefined) meta = view.meta;
  if (typeof renderMarkdown !== 'function') {
    container.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>';
    return;
  }
  if (container._reportSpyObs) { try { container._reportSpyObs.disconnect(); } catch (e) {} container._reportSpyObs = null; }

  // Capture reading position BEFORE we wipe the DOM, so a repaint (notably an
  // EN/中 language toggle, which fully rebuilds) restores the reader's place
  // instead of snapping to the top. We anchor on the heading nearest the top of
  // the viewport (heading ORDER is preserved across languages, unlike raw
  // scrollTop which is meaningless after a re-layout) + that heading's pixel
  // offset from the scroller top, so we land exactly where the eye was.
  // On a FRESH render (tab open / reload) the container isn't scrolled yet, so
  // the in-DOM capture returns null — fall back to the position persisted for
  // THIS view+language, restoring the last-read place across tab switches and
  // hard refreshes (see _persistReadingPosition).
  var _readAnchor = runtimeScope._captureReadingAnchor(container)
    || runtimeScope._loadReadingPosition(view);

  var article = document.createElement('article');
  article.className = 'paper-report-article';
  article.innerHTML = renderMarkdown(text || '');
  // Citation-integrity card — only present when the server flagged suspicious
  // citations (meta.citationAudit attached only in that case). Prepended so a
  // hallucinated-reference warning sits at the very top of the report.
  // Warning cards are prepended so they sit at the very top of the report.
  // `afterbegin` reverses insertion order, so prepend the terminology card
  // FIRST and the citation card LAST — that lands citation-integrity above
  // terminology when both fire.
  if (meta && meta.terminologyAudit) {
    var termHtml = _renderTerminologyAuditCard(meta.terminologyAudit);
    if (termHtml) article.insertAdjacentHTML('afterbegin', termHtml);
  }
  if (meta && meta.citationAudit) {
    var auditHtml = _renderCitationAuditCard(meta.citationAudit);
    if (auditHtml) article.insertAdjacentHTML('afterbegin', auditHtml);
  }
  // Rebuttal score-decision card — only present on a rebuttal report (meta
  // carries the parsed {origOverall,newOverall,...} verdict). Prepended so the
  // OA/Confidence change sits at the very top of the reviewer's reply.
  if (meta && meta.rebuttalDecision && meta.rebuttalDecision.present) {
    var decHtml = _renderRebuttalDecisionCard(meta.rebuttalDecision);
    if (decHtml) article.insertAdjacentHTML('afterbegin', decHtml);
  }
  _decorateCallouts(article);
  _frameFigures(article);
  _decorateZoomableImages(article);
  _decorateGlossaryTerms(article, _extractGlossary(article));
  var finishTag = _renderReportFinishTag(meta);
  if (finishTag) {
    var tagWrap = document.createElement('div');
    tagWrap.innerHTML = finishTag;
    if (tagWrap.firstChild) article.appendChild(tagWrap.firstChild);
  }
  var entries = _indexHeadings(article);
  var tocHTML = _buildReportTOC(entries);

  // Inline tool timeline (live session only): a freshly finished generation
  // keeps its tool/thinking timeline ABOVE the final body, exactly like a
  // settled chat agent bubble keeps its tool panel above the deliverable.
  // Reopened/cached reports have no live stream — no timeline (segments are
  // not persisted; accepted simplification).
  var _timelineHtml = '';
  try {
    var _st = view.stream;
    if (_st && _st.toolRounds && _st.toolRounds.length
        && typeof renderSegmentTimelineHTML === 'function') {
        var _finalSegs = (runtimeScope._reportSegmentsForRender || _reportSegmentsForRender)(_st);
      _timelineHtml = renderSegmentTimelineHTML(_finalSegs, { toolRounds: _st.toolRounds }, 0)
        || (typeof renderToolRoundsHTML === 'function'
            ? renderToolRoundsHTML(_st.toolRounds, false, _finalSegs) : '');
    }
  } catch (e) {
    console.warn('[Paper:Report] timeline render failed (non-fatal):', e);
  }

  container.classList.add('paper-report-enhanced');
  // Reading-time bar: sticky at the top of the scroll container, above the
  // doc/article. Built before mount so we can measure the article's word
  // count, then tracking is wired after the DOM is in place (so scrollHeight
  // is real).
  var readBar = _buildReadingTimeBar(article, container);
  if (tocHTML) {
    var doc = document.createElement('div');
    doc.className = 'paper-report-doc';
    doc.innerHTML = tocHTML;
    doc.appendChild(article);
    container.innerHTML = '';
    if (_timelineHtml) container.insertAdjacentHTML('beforeend', _timelineHtml);
    if (readBar) container.appendChild(readBar);
    container.appendChild(doc);
    _wireReportScrollSpy(container, article, doc.querySelector('.paper-report-toc'));
  } else {
    container.innerHTML = '';
    if (_timelineHtml) container.insertAdjacentHTML('beforeend', _timelineHtml);
    if (readBar) container.appendChild(readBar);
    container.appendChild(article);
  }
  // Restore the pre-repaint reading position (see _captureReadingAnchor).
  runtimeScope._restoreReadingAnchor(container, article, _readAnchor);
  if (readBar) _wireReadingTimeTracking(readBar, container, view);
  // Reading-experience rail: distribute anchored insight cards + recap
  // (no-op unless view._xpInsight carries a v2 payload). AFTER tracking is
  // wired so inserted cards count toward neither the word estimate nor the
  // anchor math of this paint.
  if (typeof runtimeScope._paperXpAfterRender === 'function') {
    runtimeScope._paperXpAfterRender(article, container, view);
  }
}

/** Paint a view's tab DOM from its current stream state. `view` defaults to
 *  the report view (historical callers pass nothing). */
function _paintReportFromState(view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container || !view.stream) return;
  var s = view.stream;
  var px = view.idPrefix;
  var retryFn = view.kind === 'review'
    ? '_generatePaperReview()'
    : (view.kind === 'rebuttal' ? '_generatePaperRebuttal()' : '_generatePaperReport()');

  // Keep the toolbar's Stop/Regenerate affordance in sync with every paint.
  _syncReportToolbar(s.status === 'running', view);

  // Terminal: done → render the final, enhanced report (once).
  if (s.status === 'done' && s.fullText && !s.toolRounds.some(r => r.status === 'searching')) {
    if (s._lastRenderedLen !== s.fullText.length || s._lastRenderedStatus !== 'done') {
      _renderFinalReport(container, s.fullText, undefined, view);
      s._lastRenderedLen = s.fullText.length;
      s._lastRenderedStatus = 'done';
    }
    return;
  }

  // Terminal: aborted → freeze the partial report (if any) under a "stopped"
  // banner. Never persisted; a Regenerate is required to produce a full report.
  if (s.status === 'aborted') {
    if (s._lastRenderedStatus !== 'aborted') {
      var bannerHtml =
        '<div class="paper-report-stopped-banner">' +
          '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>' +
          '<span>' + escapeHtml((typeof t === 'function') ? t('paper.reportStopped') : 'Generation stopped') + '</span>' +
        '</div>';
      if (s.fullText && s.contentStarted) {
        container.innerHTML = bannerHtml +
          '<div class="paper-report-body">' +
            (typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>') +
          '</div>';
      } else {
        container.innerHTML =
          '<div class="paper-report-empty">' + bannerHtml +
            '<p class="paper-report-hint">' + escapeHtml((typeof t === 'function') ? t('paper.reportStoppedHint') : 'Click Regenerate to start over') + '</p>' +
          '</div>';
      }
      s._lastRenderedStatus = 'aborted';
    }
    return;
  }

  // Ensure skeleton exists
  if (!document.getElementById(px + 'ToolZone')) {
    _renderReportSkeleton(container, s.lang, view);
  }

  // Tool rounds + thinking — rendered through the chat inline tool
  // timeline (renderSegmentTimelineHTML) so reasoning sits adjacent to the
  // most recent tool calls, exactly like a chat agent bubble. Falls back to
  // the grouped panel when the timeline can't resolve the rounds.
  var toolZone = document.getElementById(px + 'ToolZone');
  if (toolZone) {
    var toolCount = s.toolRounds.length;
    var searchingCount = s.toolRounds.filter(r => r.status === 'searching').length;
    var toolKey = toolCount + ':' + searchingCount + ':' + s.segments.length
      + ':' + s.thinkingText.length + ':' + s.fullText.length;
    if (s._lastToolKey !== toolKey) {
      if (toolCount > 0 && typeof renderSegmentTimelineHTML === 'function') {
        var _segs = (runtimeScope._reportSegmentsForRender || _reportSegmentsForRender)(s);
        toolZone.innerHTML =
          renderSegmentTimelineHTML(_segs, { toolRounds: s.toolRounds }, 0)
          || (typeof renderToolRoundsHTML === 'function'
              ? renderToolRoundsHTML(s.toolRounds, s.status === 'running', _segs) : '');
      } else if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
        toolZone.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
      } else {
        toolZone.innerHTML = '';
      }
      s._lastToolKey = toolKey;
    }
  }

  // Thinking — the standalone strip is the PRE-TOOL placeholder only: once
  // the first tool call lands, the timeline panel carries every thinking
  // segment (including the pre-tool one), so the strip must get out of the
  // way or the same reasoning shows twice.
  if (s.thinkingText) {
    var thBlock = document.getElementById(px + 'ThinkingBlock');
    var thBody = document.getElementById(px + 'ThinkingBody');
    if (thBlock) {
      thBlock.style.display = s.toolRounds.length ? 'none' : '';
      if (s.contentStarted) thBlock.open = false;
    }
    if (thBody && thBody.textContent.length !== s.thinkingText.length) {
      thBody.textContent = s.thinkingText;
      thBody.scrollTop = thBody.scrollHeight;
    }
  }

  // Report body — only re-render when content actually changed
  var bodyEl = document.getElementById(px + 'BodyContent');
  if (bodyEl) {
    if (s.contentStarted) {
      if (s._lastRenderedLen !== s.fullText.length) {
        bodyEl.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>';
        s._lastRenderedLen = s.fullText.length;
      }
    } else if (s.status === 'error' && !s.fullText) {
      bodyEl.innerHTML = '<div class="paper-error">' + escapeHtml(s.error || 'Failed') +
        '<br><button data-tofu-action="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
    }
    // Otherwise keep the loading spinner from the skeleton
  }
}






// ── Report Model Picker ──

/** Populate a view's model dropdown from _registeredModels (set by main.js).
 *  `view` defaults to the report view (historical callers pass nothing). */
function _populatePaperReportModelDropdown(view) {
  view = view || _reportView('report');
  var dropdown = document.getElementById(view.modelDropdownId);
  if (!dropdown) return;
  var pickerState = typeof _paperModelPickerState === 'function'
    ? _paperModelPickerState()
    : {
        models: globalThis._registeredModels,
        hiddenModels: globalThis._hiddenModels,
        selectedModel: (globalThis.config && globalThis.config.model)
          || globalThis.serverModel || '',
      };
  var models = Array.isArray(pickerState.models) ? pickerState.models : [];
  var hiddenSet = pickerState.hiddenModels instanceof Set
    ? pickerState.hiddenModels : new Set();

  dropdown.innerHTML = '';

  // Filter to chat-capable visible models. isChatModel is the SSOT-backed
  // predicate from the typed owner; its compiled fallback stays aligned with
  // the backend until a server taxonomy payload replaces it.
  var chatModels = models.filter(function(m) {
    if (hiddenSet.has(m.model_id)) return false;
    return (typeof runtimeScope.isChatModel === 'function') ? runtimeScope.isChatModel(m) : true;
  });

  // No "Default (auto)" option — generation should always use a specific,
  // user-visible model. When nothing has been chosen yet, default to the
  // model the user picked in the frontend toolbar preset (config.model, then
  // the configured serverModel), so Reading/Review Mode stays consistent with
  // the rest of the app. Fall back to the first visible chat model only when
  // that preset isn't among the available chat models.
  if (!view.model && chatModels.length > 0) {
    var availableIds = {};
    for (var ci = 0; ci < chatModels.length; ci++) availableIds[chatModels[ci].model_id] = true;
    var preset = String(pickerState.selectedModel || '');
    var seed = (preset && availableIds[preset]) ? preset : chatModels[0].model_id;
    _selectPaperReportModel(seed, view);
  }

  // Group by provider
  var grouped = {};
  for (var i = 0; i < chatModels.length; i++) {
    var m = chatModels[i];
    var pid = m.provider_id || 'default';
    if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
    grouped[pid].models.push(m);
  }

  /* Order both axes the way the user READS them, via the SAME shared
   * comparator the toolbar picker uses (_compareModelsByDisplayName from the
   * typed model-display owner): provider sections by provider name, in-section
   * models by display name (_modelShortName — NOT the raw model_id, which
   * sorts `yuju-claude-opus-5-evaDaily` under 'y' while rendering as
   * "Claude Opus 5"). Both pickers read the same _registeredModels, so
   * routing through one comparator keeps the two lists from ever
   * disagreeing. The guard keeps isolated alternate entries usable when they
   * omit the composed policy. */
  var _canSort = (typeof _compareModelsByDisplayName === 'function');
  var pids = Object.keys(grouped);
  if (_canSort) {
    pids.sort(function(x, y) {
      var nx = String((grouped[x] && grouped[x].name) || x);
      var ny = String((grouped[y] && grouped[y].name) || y);
      return _compareModelsByDisplayName(nx, ny);
    });
  }
  for (var pi = 0; pi < pids.length; pi++) {
    var group = grouped[pids[pi]];
    if (_canSort) group.models.sort(_compareModelsByDisplayName);
    if (pids.length > 1) {
      var section = document.createElement('div');
      section.className = 'paper-report-model-dropdown-section';
      section.textContent = group.name;
      dropdown.appendChild(section);
    }
    for (var mi = 0; mi < group.models.length; mi++) {
      var mod = group.models[mi];
      var item = document.createElement('div');
      item.className = 'paper-report-model-dropdown-item' + (mod.model_id === view.model ? ' active' : '');
      var shortName = (typeof _modelShortName === 'function') ? _modelShortName(mod.model_id) : mod.model_id;
      item.textContent = shortName;
      item.title = mod.model_id;
      (function(mid) {
        item.onclick = function() { _selectPaperReportModel(mid, view); };
      })(mod.model_id);
      dropdown.appendChild(item);
    }
  }
}

function _selectPaperReportModel(modelId, view) {
  view = view || _reportView('report');
  view.model = modelId || '';
  // Update label — always show the actual model, never "Default"
  var label = document.getElementById(view.modelLabelId);
  if (label) {
    if (modelId) {
      label.textContent = (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
      /* The markup ships data-i18n="paper.reportSelectModel" for the initial
       * placeholder. Once a real model is chosen that attribute must go: the
       * next _applyI18n() (language toggle, and it also runs on boot) walks
       * every [data-i18n] and would overwrite the model name with "Select
       * model" — losing the one piece of state this button exists to show. */
      label.removeAttribute('data-i18n');
      /* Long ids are ellipsized by CSS; the button's tooltip carries the full
       * id so it stays recoverable. Set it on the BUTTON (the label span is
       * the clipped box) and drop the static data-i18n-title for the same
       * clobber reason as above. */
      var btn = label.closest('.paper-report-model-btn');
      if (btn) {
        btn.removeAttribute('data-i18n-title');
        btn.title = modelId;
      }
    } else {
      // No model available (empty model list) — keep the button usable.
      label.textContent = (typeof t === 'function') ? t('paper.reportSelectModel') : 'Select model';
    }
  }
  // Close dropdown
  var dropdown = document.getElementById(view.modelDropdownId);
  if (dropdown) dropdown.classList.remove('open');
  // Update active state
  var items = dropdown ? dropdown.querySelectorAll('.paper-report-model-dropdown-item') : [];
  items.forEach(function(it) { it.classList.toggle('active', it.title === modelId); });
}

function _togglePaperReportModelDropdown(e, view) {
  e.stopPropagation();
  view = view || _reportView('report');
  var dropdown = document.getElementById(view.modelDropdownId);
  if (!dropdown) return;
  var isOpen = dropdown.classList.contains('open');
  if (!isOpen) _populatePaperReportModelDropdown(view);
  dropdown.classList.toggle('open');
}

/** Review-Mode model dropdown toggle (inline onclick passes the event). */
function _togglePaperReviewModelDropdown(e) {
  return _togglePaperReportModelDropdown(e, _reportView('review'));
}

/** Keep a glossary hover-card fully inside its reading column.
 *
 * The card is `position:absolute; left:0` (its own left edge tracks the term's
 * left edge). For a term near the RIGHT edge of a narrow column — routinely the
 * case on a portrait tablet or a single-pane phone — the 320px card overflows
 * the pane. With `overflow-x:hidden` on the reader pane that overflow is now
 * CLIPPED (instead of the old drag-to-see-it behaviour). So on reveal we shift
 * the card left by a negative inline `left` just enough to sit inside the
 * scroller's content box (8px margin), clamped so it never leaves past the
 * term's own left edge on the other side. Recomputed on every reveal (widths
 * change with the language toggle / font-scale). */
function _positionGlossaryCard(term) {
  if (!term) return;
  var card = term.querySelector(':scope > .paper-term-card');
  if (!card) return;
  var scroller = term.closest('.paper-report-content, .paper-report-body');
  if (!scroller) return;
  card.style.left = '';   // reset to the CSS default (0) before measuring
  var termRect = term.getBoundingClientRect();
  var scRect = scroller.getBoundingClientRect();
  var cardW = card.offsetWidth;
  var MARGIN = 8;
  // Default card-left in viewport coords == term's left edge.
  var minLeft = scRect.left + MARGIN;
  var maxLeft = scRect.right - MARGIN - cardW;
  var desired = termRect.left;
  if (maxLeft < minLeft) desired = minLeft;                 // card wider than column
  else desired = Math.min(Math.max(desired, minLeft), maxLeft);
  var offset = desired - termRect.left;
  if (offset) card.style.left = offset + 'px';
}
document.addEventListener('mouseover', function(e) {
  var t = e.target && e.target.closest && e.target.closest('.paper-term');
  if (t) _positionGlossaryCard(t);
});
document.addEventListener('focusin', function(e) {
  var t = e.target && e.target.closest && e.target.closest('.paper-term');
  if (t) _positionGlossaryCard(t);
});

// Close model dropdowns on outside click (both views).
document.addEventListener('click', function() {
  ['paperReportModelDropdown', 'paperReviewModelDropdown'].forEach(function(id) {
    var dropdown = document.getElementById(id);
    if (dropdown) dropdown.classList.remove('open');
  });
});

// Click-to-enlarge for figures/tables embedded in the paper report. CSS
// already shows ``cursor:zoom-in`` on these images; this handler wires
// them up to the shared fullscreen overlay used by image-gen.
document.addEventListener('click', function(e) {
  var img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  if (!img.closest('.paper-report-body, .paper-report-content')) return;
  if (typeof _openImageFullscreen === 'function') {
    _openImageFullscreen(img.src);
  }
});

// Keyboard path for the same figures: report images are made focusable
// (tabindex=0 + role=button, in _decorateZoomableImages) so a keyboard user can
// Tab to a figure and press Enter/Space to open the same fullscreen overlay.
document.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  var img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  if (!img.closest('.paper-report-body, .paper-report-content')) return;
  e.preventDefault();
  if (typeof _openImageFullscreen === 'function') _openImageFullscreen(img.src);
});

/** Make every report image keyboard-operable: focusable + button semantics so
 *  the Enter/Space handler above (and screen readers) can reach the zoom the
 *  mouse gets via cursor:zoom-in. Idempotent. */
function _decorateZoomableImages(root) {
  if (!root) return;
  var imgs = root.querySelectorAll('img');
  for (var i = 0; i < imgs.length; i++) {
    var im = imgs[i];
    if (im.getAttribute('tabindex') === null) im.setAttribute('tabindex', '0');
    if (!im.getAttribute('role')) im.setAttribute('role', 'button');
    if (!im.getAttribute('aria-label')) {
      var alt = (im.getAttribute('alt') || '').trim();
      im.setAttribute('aria-label', (alt ? alt + ' — ' : '') +
        ((typeof t === 'function') ? t('paper.imageZoomHint') : 'enlarge image'));
    }
  }
}


/** Show the Stop button while a report task is running; otherwise show
 *  Regenerate. Mirrors the chat composer's send↔stop morph so the affordance
 *  is consistent across the app. `running` defaults to the live stream status. */
function _syncReportToolbar(running, view) {
  view = view || _reportView('report');
  if (running === undefined) {
    running = !!(view.stream && view.stream.status === 'running');
  }
  var stopBtn = document.getElementById(view.stopBtnId);
  var regenBtn = document.getElementById(view.regenBtnId);
  if (stopBtn) {
    stopBtn.style.display = running ? '' : 'none';
    if (running) {
      // A stop was already requested but the task is still `running` (the
      // server's authoritative `aborted` event hasn't landed via poll yet):
      // keep the button disabled + "Stopping…" so this repaint doesn't
      // re-enable it (which let the user click Stop again). Otherwise restore
      // the resting label/enabled state for a fresh run.
      var stopping = !!(view.stream && (view.stream.stopRequested || view.stream.pendingStop));
      stopBtn.disabled = stopping;
      var lbl = stopBtn.querySelector('span');
      if (lbl) lbl.textContent = (typeof t === 'function')
        ? t(stopping ? 'paper.reportStopping' : 'paper.reportStop')
        : (stopping ? 'Stopping…' : 'Stop');
    }
  }
  if (regenBtn) regenBtn.style.display = running ? 'none' : '';
  // Rebuttal view: the initial Generate button and Copy live in the sub-panel
  // (not the report/review toolbar). Show Generate only before a first run,
  // Regenerate + Copy only once there's output; hide Generate while running.
  if (view.kind === 'rebuttal') {
    var genBtn = document.getElementById('paperRebuttalGenBtn');
    var copyBtn = document.getElementById('paperRebuttalCopyBtn');
    var hasOutput = !!(view.cache || (view.stream && view.stream.fullText));
    if (genBtn) genBtn.style.display = (running || hasOutput) ? 'none' : '';
    if (regenBtn) regenBtn.style.display = (!running && hasOutput) ? '' : 'none';
    if (copyBtn) copyBtn.style.display = (!running && hasOutput) ? '' : 'none';
    // Light the Rebuttal segment dot as soon as a follow-up reply exists, so
    // the reviewer sees it without switching (idempotent; does not change the
    // active segment).
    if (typeof _syncReviewSegState === 'function') { try { _syncReviewSegState(); } catch (e) {} }
  }
  // Keep the EN/中 segmented control in sync on every paint (both views).
  if (typeof runtimeScope._syncReportLangToggle === 'function') {
    runtimeScope._syncReportLangToggle(view);
  }
  // Review-only: a fresh run invalidates any cached translation reading view;
  // keep the translate toggle's state in sync on every paint.
  if (view.kind === 'review') {
    if (running) {
      runtimeScope._paperReviewShowTranslation = false;
      runtimeScope._paperReviewTranslatedText = '';
    }
    if (typeof runtimeScope._syncReviewTranslateBtn === 'function') {
      runtimeScope._syncReviewTranslateBtn();
    }
  }
}

function _copyPaperReport(view) {
  view = view || _reportView('report');
  if (!view.cache) return;
  navigator.clipboard.writeText(view.cache).then(function() { debugLog((typeof t === 'function') ? t('paper.reportCopied') : 'Copied', 'success'); });
}

function _copyPaperReview() { return _copyPaperReport(_reportView('review')); }

function _copyPaperRebuttal() { return _copyPaperReport(_reportView('rebuttal')); }

/** Killer feature: auto-fill the review form on the reviewer's OPEN OpenReview
 *  tab from the generated review, then STOP (server never clicks Submit). The
 *  human reviews the filled form and submits it themselves. Requires (a) the
 *  browser bridge extension connected, (b) an existing review for this venue,
 *  and (c) the active tab being an OpenReview page — each failure returns a
 *  clear, actionable toast rather than a silent no-op. */
async function _autofillOpenReview() {
  if (!runtimeScope._activePaperId || !_paperHash) {
    if (typeof showToast === 'function') showToast((typeof t === 'function') ? t('paper.autofillNoPaper') : 'Open a paper first');
    return;
  }
  var rv = _reportView('review');
  if (!rv.cache && !(rv.stream && rv.stream.fullText)) {
    if (typeof showToast === 'function') showToast((typeof t === 'function') ? t('paper.rebuttalNeedReview') : 'Generate the review first');
    return;
  }
  var btn = document.getElementById('paperReviewAutofillBtn');
  if (btn) btn.disabled = true;
  try {
    if (typeof showToast === 'function') showToast((typeof t === 'function') ? t('paper.autofillWorking') : 'Filling the OpenReview form…');
    var report = await Api.paper.openreviewAutofill({
      paper_hash: _paperHash,
      venue: _paperReviewVenue || 'generic',
      ui_lang: 'en',
    });
    if (report && report.ok) {
      var n = (report.filled || []).filter(function(f) { return f.ok; }).length;
      var msg = (typeof t === 'function')
        ? t('paper.autofillDone', { n: n })
        : ('Filled ' + n + ' field(s). NOT submitted — review the form and click Submit yourself.');
      if (typeof showToast === 'function') showToast(msg, 'success');
    } else {
      // Server returned ok:false with an actionable message (not connected /
      // not an OpenReview page / no form). Surface it verbatim.
      var emsg = (report && report.message) || ((typeof t === 'function') ? t('paper.autofillFailed') : 'Auto-fill could not complete');
      if (typeof showToast === 'function') showToast(emsg, 'error');
    }
  } catch (e) {
    console.warn('[Paper:OpenReview] autofill failed:', e);
    var fmsg = (e && e.message) ? e.message : ((typeof t === 'function') ? t('paper.autofillFailed') : 'Auto-fill could not complete');
    if (typeof showToast === 'function') showToast(fmsg, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _togglePaperReportExportMenu(ev, view) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  view = view || _reportView('report');
  var dd = document.getElementById(view.exportDropdownId);
  if (!dd) return;
  var menuSel = '#' + view.exportMenuId;
  var willOpen = !dd.classList.contains('open');
  dd.classList.toggle('open', willOpen);
  if (willOpen) {
    var closeOnClick = function(e) {
      if (!dd.contains(e.target) && !e.target.closest(menuSel)) {
        dd.classList.remove('open');
        document.removeEventListener('click', closeOnClick, true);
      }
    };
    setTimeout(function() { document.addEventListener('click', closeOnClick, true); }, 0);
  }
}

function _togglePaperReviewExportMenu(ev) { return _togglePaperReportExportMenu(ev, _reportView('review')); }

/** Export the report/review. format ∈ {'md','html','pdf'}. Defaults to 'md'.
 *  All rendering happens server-side via /api/paper/report/export so the
 *  same Markdown→HTML pipeline serves both download and live view, and
 *  there's no client/server skew. PDF is rendered by the browser's
 *  built-in print engine over the server-generated HTML. The composite
 *  langKey routes a review export to the stored review row. */
function _exportPaperReport(format, view) {
  view = view || _reportView('report');
  if (!_paperHash) {
    debugLog('No report to export yet', 'warning');
    return;
  }
  var dd = document.getElementById(view.exportDropdownId);
  if (dd) dd.classList.remove('open');
  format = format || 'md';
  var url = Api.paper.exportUrl(_paperHash, view.langKey(), format);

  if (format === 'pdf') {
    // Server returns inline HTML with an embedded window.print() bootstrap
    // that fires after all images load. The user picks "Save as PDF" in
    // their browser's print dialog. (Returning Content-Disposition:
    // attachment for HTML would download the file instead of opening it,
    // so the format=pdf path is explicitly served inline by the server.)
    var w = window.open(url, '_blank');
    if (!w) {
      debugLog('Pop-up blocked — please allow pop-ups to print/export PDF', 'warning');
    }
    return;
  }

  // Markdown / HTML — direct browser download via Content-Disposition.
  var a = document.createElement('a');
  a.href = url;
  a.rel = 'noopener';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

function _exportPaperReview(format) { return _exportPaperReport(format, _reportView('review')); }

// ── Review venue picker ──

/** Read the per-paper venue map { paperId: venueKey } from localStorage. */
function _readVenueMap() {
  try {
    var raw = localStorage.getItem(_PAPER_REVIEW_VENUE_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper:Review] read venue map failed:', e);
    return {};
  }
}

/** Persist the venue the user picked for the active paper, so re-opening the
 *  Review tab (or hard-refreshing) restores it instead of snapping back to the
 *  first venue — same durability the model selection gets. */
function _persistReviewVenue(paperId, venueKey) {
  if (!paperId || !venueKey) return;
  try {
    var map = _readVenueMap();
    map[paperId] = venueKey;
    localStorage.setItem(_PAPER_REVIEW_VENUE_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper:Review] persist venue failed:', e);
  }
}

/** Persist the venue a review was ACTUALLY generated/cached under, derived from
 *  the composite langKey (``review:<venue>:<uilang>``) that keyed the DB row.
 *  Called on every terminal-success path (done / cached / cache-hit) so a
 *  reload resolves the SAME venue and its lookup key matches the stored row —
 *  the fix for "already generated but re-prompts Generate". Unlike
 *  _persistReviewVenue-on-explicit-click, this persists the venue that was
 *  effectively used even when it came from the silent registry-first default,
 *  because that default is what the review is now stored under. Report views
 *  and malformed keys are ignored. */
function _persistGeneratedReviewVenue(view, langKey, paperId) {
  if (!view || view.kind !== 'review') return;
  paperId = paperId || runtimeScope._activePaperId;
  if (!paperId) return;
  var parts = String(langKey || '').split(':');
  if (parts[0] !== 'review' || !parts[1]) return;
  _persistReviewVenue(paperId, parts[1]);
}

/** Fetch the venue list (once) into _paperReviewVenues. Idempotent — a second
 *  call after the list is cached resolves immediately with no network. */
async function _ensureReviewVenues() {
  if (_paperReviewVenues.length) return _paperReviewVenues;
  try {
    // Api.paper.reviewVenues() uses the default JSON parse, so it resolves to
    // the parsed {ok, venues} body — NOT a raw Response. (Calling .json() on
    // it threw, silently leaving the venue list empty → dropdown unavailable.)
    var data = await Api.paper.reviewVenues();
    if (data && data.ok && Array.isArray(data.venues)) _paperReviewVenues = data.venues;
  } catch (e) {
    console.warn('[Paper:Review] venue fetch failed:', e);
  }
  return _paperReviewVenues;
}

/** Resolve the venue to use for the ACTIVE paper BEFORE any generation. Order:
 *  (1) an explicit in-session choice; (2) the persisted per-paper venue;
 *  (3) the first venue in the registry. Returns the resolved key (also sets
 *  _paperReviewVenue + the dropdown label) so the displayed venue and the
 *  generation langKey are ALWAYS consistent — closing the first-entry race
 *  where label said NeurIPS but the key was the generic fallback. */
async function _resolveReviewVenue() {
  await _ensureReviewVenues();
  if (!_paperReviewVenues.length) return _paperReviewVenue;  // nothing to pick from
  if (!_paperReviewVenue) {
    var stored = _readVenueMap()[runtimeScope._activePaperId];
    var valid = stored && _paperReviewVenues.some(function(v) { return v.key === stored; });
    _selectReviewVenue(valid ? stored : _paperReviewVenues[0].key, true);
  }
  return _paperReviewVenue;
}

/** Populate the review venue dropdown from /api/paper/review/venues (single
 *  source of truth: REVIEW_VENUES in lib/paper/review.py). Cached after first
 *  fetch. Auto-selects the first venue if none is chosen yet. */
async function _populateReviewVenueDropdown() {
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (!dropdown) return;
  // Resolve the per-paper venue (persisted → first) BEFORE rendering so the
  // label and the generation langKey agree from the very first paint.
  await _resolveReviewVenue();
  dropdown.innerHTML = '';
  for (var i = 0; i < _paperReviewVenues.length; i++) {
    var v = _paperReviewVenues[i];
    var item = document.createElement('div');
    item.className = 'paper-report-model-dropdown-item' + (v.key === _paperReviewVenue ? ' active' : '');
    item.textContent = v.name;
    item.title = v.key;
    (function(key) { item.onclick = function() { _selectReviewVenue(key); }; })(v.key);
    dropdown.appendChild(item);
  }
}

/** Select a review venue. `silent` skips the label/dropdown DOM churn during
 *  auto-init. Changing the venue changes the composite cache key, so a fresh
 *  review is loaded/generated for the new venue (each venue cached separately). */
function _selectReviewVenue(key, silent) {
  var changed = (_paperReviewVenue !== key);
  _paperReviewVenue = key || '';
  // Persist per-paper ONLY for an EXPLICIT user choice (not the silent
  // auto-default). The auto-default is re-derived identically on every entry
  // (first-in-registry), so persisting it would (a) pin every merely-viewed
  // paper to the current default and (b) write localStorage on every tab open.
  // We only want to remember a venue the user deliberately picked.
  if (key && runtimeScope._activePaperId && !silent) _persistReviewVenue(runtimeScope._activePaperId, key);
  var label = document.getElementById('paperReviewVenueLabel');
  if (label) {
    var found = _paperReviewVenues.find(function(v) { return v.key === key; });
    label.textContent = found ? found.name : ((typeof t === 'function') ? t('paper.reviewSelectVenue') : 'Select venue');
  }
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (dropdown) {
    dropdown.classList.remove('open');
    dropdown.querySelectorAll('.paper-report-model-dropdown-item').forEach(function(it) {
      it.classList.toggle('active', it.title === key);
    });
  }
  // A real venue switch (not the silent auto-init) re-loads the review for the
  // newly-selected venue — its cache key differs, so this never clobbers
  // another venue's stored review.
  if (changed && !silent && _paperActiveTab === 'review') {
    runtimeScope._resetReportLocalState(_reportView('review'));
    _paperReviewCache = '';
    runtimeScope._loadOrGenerateReport(_reportView('review'));
  }
}

function _toggleReviewVenueDropdown(e) {
  if (e) e.stopPropagation();
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (!dropdown) return;
  var isOpen = dropdown.classList.contains('open');
  if (!isOpen) _populateReviewVenueDropdown();
  dropdown.classList.toggle('open');
}

// Close the venue dropdown on outside click.
document.addEventListener('click', function() {
  var dd = document.getElementById('paperReviewVenueDropdown');
  if (dd) dd.classList.remove('open');
});

/* Explicit report renderer surface consumed by the native task owner. */
if (typeof window !== 'undefined') {
  // Domain bundles are free to isolate top-level declarations. Publish the
  // small renderer/reducer surface the native task owner deliberately calls.
  runtimeScope._segApplyDeltaReset = _segApplyDeltaReset;
  runtimeScope._reportSegmentsForRender = _reportSegmentsForRender;
  runtimeScope._applyReportEventRaw = _applyReportEventRaw;
  runtimeScope._attachReportPush = _attachReportPush;
  runtimeScope._detachReportPush = _detachReportPush;
  runtimeScope._paintReportFromState = _paintReportFromState;
  runtimeScope._persistGeneratedReviewVenue = _persistGeneratedReviewVenue;
  runtimeScope._populateReviewVenueDropdown = _populateReviewVenueDropdown;
  runtimeScope._populatePaperReportModelDropdown = _populatePaperReportModelDropdown;
  runtimeScope._teardownReadingTracker = _teardownReadingTracker;
  runtimeScope._renderFinalReport = _renderFinalReport;
  runtimeScope._renderReportSkeleton = _renderReportSkeleton;
  runtimeScope._resolveReviewVenue = _resolveReviewVenue;
  runtimeScope._syncReportToolbar = _syncReportToolbar;
  runtimeScope._autofillOpenReview = _autofillOpenReview;
  runtimeScope._copyPaperRebuttal = _copyPaperRebuttal;
  runtimeScope._copyPaperReport = _copyPaperReport;
  runtimeScope._copyPaperReview = _copyPaperReview;
  runtimeScope._exportPaperReport = _exportPaperReport;
  runtimeScope._exportPaperReview = _exportPaperReview;
  runtimeScope._scrollReportToHeading = _scrollReportToHeading;
  runtimeScope._togglePaperReportExportMenu = _togglePaperReportExportMenu;
  runtimeScope._togglePaperReportModelDropdown = _togglePaperReportModelDropdown;
  runtimeScope._togglePaperReviewExportMenu = _togglePaperReviewExportMenu;
  runtimeScope._togglePaperReviewModelDropdown = _togglePaperReviewModelDropdown;
  runtimeScope._toggleReviewVenueDropdown = _toggleReviewVenueDropdown;
  // Backing refs: an accessor whose getter reads the property it defines
  // is self-recursive — every read is RangeError (detonated at boot by the
  // tail publish loop's key walk, 2026-08-15). The refs keep the
  // override-without-rebinding semantics the table was after.
  let _segApplyDeltaResetRef = _segApplyDeltaReset;
  let _reportSegmentsForRenderRef = _reportSegmentsForRender;
  Object.defineProperties(runtimeScope, {
    _segApplyDeltaReset: {
      configurable: true,
      get: () => _segApplyDeltaResetRef,
      set: (value) => { _segApplyDeltaResetRef = value; },
    },
    _reportSegmentsForRender: {
      configurable: true,
      get: () => _reportSegmentsForRenderRef,
      set: (value) => { _reportSegmentsForRenderRef = value; },
    },
  });
}
/* ===== migrated source: paper-reader.js ===== */
/* ═══════════════════════════════════════════
   paper-reader.js — Paper Reading Mode v3

   Layout:  Sidebar  = paper library (persistent)
            Main L   = PDF (vertical scroll, largest)
            Main R   = Q&A / Report / Babel PDF
   ═══════════════════════════════════════════ */

// ── State ──
var paperMode = false;
Object.defineProperty(runtimeScope, 'paperMode', {
  configurable: true,
  get: () => paperMode,
  set: (enabled) => { paperMode = !!enabled; },
});
var _paperDescribeDraft = '';  // persists the landing "describe it" textarea across mode switches
function _setPaperDescribeDraft(input) {
  _paperDescribeDraft = String(input?.value || '');
}
var _paperPdfUrl = '';
var _paperFileName = '';
var _paperParsedText = '';
var _paperArxivId = '';
var _paperPdfDoc = null;
var _paperTotalPages = 0;
var _paperScale = 1.5;
var _paperCurrentUrl = '';          // resolved URL of the doc currently loaded (for {data} re-open)
var _paperViaData = false;          // true once the doc was (re)opened via the range-bypass {data} path
var _paperRenderToken = 0;          // bumped each _renderAllPages() so a stale in-flight loop self-cancels
var _paperLoadGen = 0;              // bumped each _loadPaperPdf() so a stale/slow load can't clobber a newer sidebar selection
var _paperIntersectionObserver = null;  // lazy-raster trigger for virtualized pages
var _paperReopenInFlight = false;   // single-flight guard for the raster-failure {data} re-open
var _paperActiveTab = 'qa';
var _paperReportCache = '';
var _paperReportMeta = null;  // finish-tag: {model, costCny, costUsd, promptTokens, ...}
var _paperHash = '';  // server-side hash for DB report cache lookup
Object.defineProperties(runtimeScope, {
  _paperParsedText: {
    configurable: true,
    get: () => _paperParsedText,
    set: (value) => { _paperParsedText = String(value || ''); },
  },
  _paperHash: {
    configurable: true,
    get: () => _paperHash,
    set: (value) => { _paperHash = String(value || ''); },
  },
});

// localStorage key holding a pending "regenerate in progress for (paperHash,
// lang)" intent. Written synchronously BEFORE the force /start round-trip so a
// refresh that interrupts that request still resumes the regenerate instead of
// reverting to the stale cached report (see runtimeScope._setReportRegenIntent).
var _REPORT_REGEN_INTENT_KEY = 'paper_report_regen_intent';

var _paperQAHistory = [];
var _paperLoading = false;
var _paperQAStreaming = false;
var _paperQAAbort = null;
var _paperQAAbortRequested = false;  // set by the Stop button to break the QA poll loop
var _paperReportModel = '';  // user-selected model for report generation
var _paperImages = [];  // [{url, caption, page, source, width, height}] — for embedding in report
var _paperPdfFilename = '';  // server-side PDF filename — handed back from /api/paper/upload and /api/paper/fetch-arxiv-stream
var _paperSearchResults = [];  // last arXiv search candidate list (rendered on the landing screen)
var _lastArxivSearchQuery = '';  // query for the last rendered candidate list (katex:loaded repaint)

// ── Report streaming state (2026-04-18 rewrite) ──
// Server owns the report task; the frontend only polls.
// _paperReportStream mirrors the in-flight task's accumulated state for
// the currently-active paper. See the Tab 2 Report section below for
// the full lifecycle (start → poll → apply events → paint).
var _paperReportStream = null;

// ── Review Mode streaming state (2026-06-30) ──
// Review Mode is a SECOND consumer of the exact same report engine: the whole
// stream/poll/render/export stack below is parameterized by a `kind`
// ('report' | 'review') so the two never share cache, DOM, or export — but
// also never fork the polling logic (see `_reportView`). The review's server
// cache key is the composite ``review:<venue>:<uilang>``; everything else is
// identical. These four globals mirror the report's four, for the review view.
var _paperReviewCache = '';
var _paperReviewMeta = null;
var _paperReviewStream = null;
var _paperReviewModel = '';      // user-selected model for review generation
var _paperReviewVenue = '';      // selected venue key (e.g. 'neurips'); '' until resolved per-paper
var _paperReviewVenues = [];     // [{key, name}] fetched from /api/paper/review/venues
var _REVIEW_REGEN_INTENT_KEY = 'paper_review_regen_intent';
// Rebuttal follow-up is a THIRD consumer of the same report engine: given the
// existing review + the author's pasted rebuttal, it produces a reviewer
// follow-up reply + a structured score-adjustment decision. It shares the
// venue (a rebuttal is always against the same venue's review) and is cached
// under the sibling composite key ``rebuttal:<venue>:<uilang>``. Its four
// state slots mirror the report/review ones so the shared poll loop drives it.
var _paperRebuttalCache = '';
var _paperRebuttalMeta = null;
var _paperRebuttalStream = null;
var _paperRebuttalModel = '';
var _REBUTTAL_REGEN_INTENT_KEY = 'paper_rebuttal_regen_intent';
// Which Review-tab segment is on screen: 'review' (the peer review) or
// 'rebuttal' (the author-response → follow-up reply). The two are
// MUTUALLY-EXCLUSIVE full-height panels (see _setReviewSeg) so their two long
// documents never compete for the same flex-column height.
var _paperReviewSeg = 'review';
// Per-paper venue preference — persisted so the SAME paper re-opens on the
// venue the user last picked (survives tab switches AND hard refresh), like
// the model selection survives. Keyed by paper id in one localStorage map.
var _PAPER_REVIEW_VENUE_KEY = 'paper_review_venue_by_id';
// Review-translation reading view (2026-07-01). A peer review is ALWAYS
// generated in English (the language the authors/AC read); a Chinese UI can
// toggle a translated reading view. English stays canonical (cache/export);
// the translation is produced on demand by the Babel translate task under a
// DISTINCT composite lang key ``review:<venue>:<lang>`` so it never collides
// with the whole-paper Babel cache.
// Review reading language ('en' | 'zh'). English stays the canonical
// generated/cached/exported text; 'zh' shows an on-demand translated reading
// view. Persisted per paper so the last-read language re-opens. The toggle is
// always available (both directions), independent of the app UI language.
// View-context registry: the single seam that lets one set of report
// functions drive two independent tabs. Each context exposes the per-view
// DOM ids, the regenerate-intent localStorage key, the cache-key resolver,
// and get/set accessors onto that view's four state globals (so there is
// exactly ONE storage location per view — no mirrored copies that can drift).
/** Rehydrate the rebuttal sub-panel on Review-tab entry: fill the paste box
 *  from the per-paper persisted text, then resume-poll or repaint any existing
 *  rebuttal stream / in-memory cache so a tab switch or reload keeps the
 *  follow-up reply + decision on screen. Cheap + idempotent. */
function _restoreRebuttalPanel() {
  try {
    var ta = document.getElementById('paperRebuttalInput');
    var stored = typeof runtimeScope._restorePaperRebuttalInputText === 'function'
      ? runtimeScope._restorePaperRebuttalInputText()
      : '';
    if (ta) ta.value = stored;
    var rv = _reportView('rebuttal');
    if (rv.stream && rv.stream.status === 'running') {
      // Only resume-poll when no poll chain is already live for this stream.
      // Without this guard, every Review↔Rebuttal segment toggle (or tab
      // re-entry) while a follow-up is generating stacks ANOTHER setTimeout
      // poll chain on the same stream — duplicate timers, racing repaints, and
      // eventually a wedged "Generate follow-up" button. Mirrors the
      // `!view.stream.pollTimer` guard in _loadOrGenerateReport.
      if (!rv.stream.pollTimer && typeof runtimeScope._pollReportTask === 'function') runtimeScope._pollReportTask(rv);
    } else if (rv.cache || (rv.stream && rv.stream.fullText)) {
      if (typeof _paintReportFromState === 'function' && rv.stream) _paintReportFromState(rv);
      else if (typeof _renderFinalReport === 'function' && rv.cache) {
        var c = document.getElementById(rv.containerId);
        if (c) _renderFinalReport(c, rv.cache, undefined, rv);
      }
    }
  } catch (e) { console.warn('[Paper:Rebuttal] restore panel failed:', e); }
}

/** Switch the Review tab between its two mutually-exclusive segments:
 *   - 'review'   → the peer-review report (#paperReviewContent)
 *   - 'rebuttal' → the author-rebuttal paste + follow-up reply (#paperRebuttalContent)
 *  Only the chosen .paper-review-seg-panel stays in layout; the other is
 *  display:none. Because the two long documents are never co-laid-out, neither
 *  can be squeezed to 0 height by the other (the old stacked-<details> bug).
 *  Pure display toggle — containerId / cache / stream state are untouched, so
 *  the shared report-engine pipeline (_reportView, poll, paint) is unaffected.
 *  Flushes the active reading tracker on switch so one segment's reading
 *  position is never mis-attributed to the other (each keys its own langKey
 *  slot). */
function _setReviewSeg(seg) {
  if (seg !== 'review' && seg !== 'rebuttal') seg = 'review';
  // Leaving a segment: flush its reading session so the tracker for the newly
  // shown segment starts clean (the paint path re-wires it against the right
  // scroller + view). Cheap + idempotent.
  if (_paperReviewSeg !== seg && typeof _teardownReadingTracker === 'function') {
    _teardownReadingTracker(true);
  }
  _paperReviewSeg = seg;
  var root = document.querySelector('.paper-tab-panel[data-tab="review"]');
  if (!root) return;
  root.querySelectorAll('.paper-review-seg-panel').forEach(function(p) {
    p.style.display = (p.dataset.seg === seg) ? '' : 'none';
  });
  root.querySelectorAll('#paperReviewSeg .paper-review-seg-btn').forEach(function(b) {
    var on = b.dataset.seg === seg;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  // When the rebuttal segment becomes visible its scroller finally has real
  // geometry; repaint any existing follow-up so the score card + reply land.
  if (seg === 'rebuttal') {
    try { _restoreRebuttalPanel(); } catch (e) { console.warn('[Paper:Rebuttal] seg repaint failed:', e); }
  }
}

/** Reflect rebuttal state on the segment switcher: light the Rebuttal tab's
 *  status dot when a follow-up reply exists for this paper (cache or a live
 *  stream), so the reviewer sees there is something to read without switching.
 *  Reset the active segment to 'review' on paper switch / tab (re)entry so a
 *  new paper never opens straight into a stale rebuttal panel. */
function _syncReviewSegState(resetToReview) {
  if (resetToReview) _setReviewSeg('review');
  else _setReviewSeg(_paperReviewSeg);
  try {
    var dot = document.getElementById('paperRebuttalSegDot');
    if (dot) {
      var rv = _reportView('rebuttal');
      var has = !!(rv && (rv.cache || (rv.stream && rv.stream.fullText)));
      dot.style.display = has ? '' : 'none';
    }
  } catch (e) { console.warn('[Paper:Rebuttal] sync seg state failed:', e); }
}

function _reportView(kind) {
  if (kind === 'rebuttal') {
    return {
      kind: 'rebuttal', idPrefix: 'rebuttal', containerId: 'paperRebuttalContent',
      stopBtnId: 'paperRebuttalStopBtn', regenBtnId: 'paperRebuttalRegenBtn',
      copyLabelId: 'paperRebuttalCopyLabel',
      exportMenuId: 'paperRebuttalExportMenu', exportDropdownId: 'paperRebuttalExportDropdown',
      modelDropdownId: 'paperRebuttalModelDropdown', modelLabelId: 'paperRebuttalModelLabel',
      regenIntentKey: _REBUTTAL_REGEN_INTENT_KEY,
      // A rebuttal reply, like the review, is generated in English (the venue
      // discussion language); a Chinese UI reads a translated view on demand.
      uiLang: function() { return 'en'; },
      // Sibling composite key ``rebuttal:<venue>:<uilang>`` — same venue as the
      // review, never collides with the ``review:…`` row.
      langKey: function() { return 'rebuttal:' + (_paperReviewVenue || 'generic') + ':' + this.uiLang(); },
      get cache() { return _paperRebuttalCache; }, set cache(v) { _paperRebuttalCache = v; },
      get meta() { return _paperRebuttalMeta; }, set meta(v) { _paperRebuttalMeta = v; },
      get stream() { return _paperRebuttalStream; }, set stream(v) { _paperRebuttalStream = v; },
      get model() { return _paperRebuttalModel; }, set model(v) { _paperRebuttalModel = v; },
    };
  }
  if (kind === 'review') {
    return {
      kind: 'review', idPrefix: 'review', containerId: 'paperReviewContent',
      stopBtnId: 'paperReviewStopBtn', regenBtnId: 'paperReviewRegenBtn',
      copyLabelId: 'paperReviewCopyLabel',
      exportMenuId: 'paperReviewExportMenu', exportDropdownId: 'paperReviewExportDropdown',
      modelDropdownId: 'paperReviewModelDropdown', modelLabelId: 'paperReviewModelLabel',
      regenIntentKey: _REVIEW_REGEN_INTENT_KEY,
      // A peer review is ALWAYS generated in English: conference submissions
      // and the review the authors/AC read are English, so English is the
      // canonical text (cache key, lookup, export). A Chinese UI reads a
      // translated view produced on demand by the Babel translate task (see
      // _setReviewLang) — the English review underneath is never
      // regenerated per-language.
      uiLang: function() { return 'en'; },
      // Composite cache key — what the server uses to pick the review prompt
      // AND to keep reviews out of the plain (paper_hash,'en') report cache.
      langKey: function() { return 'review:' + (_paperReviewVenue || 'generic') + ':' + this.uiLang(); },
      get cache() { return _paperReviewCache; }, set cache(v) { _paperReviewCache = v; },
      get meta() { return _paperReviewMeta; }, set meta(v) { _paperReviewMeta = v; },
      get stream() { return _paperReviewStream; }, set stream(v) { _paperReviewStream = v; },
      get model() { return _paperReviewModel; }, set model(v) { _paperReviewModel = v; },
    };
  }
  return {
    kind: 'report', idPrefix: 'report', containerId: 'paperReportContent',
    stopBtnId: 'paperReportStopBtn', regenBtnId: 'paperReportRegenBtn',
    copyLabelId: 'paperReportCopyLabel',
    exportMenuId: 'paperReportExportMenu', exportDropdownId: 'paperReportExportDropdown',
    modelDropdownId: 'paperReportModelDropdown', modelLabelId: 'paperReportModelLabel',
    regenIntentKey: _REPORT_REGEN_INTENT_KEY,
    // Report generation language is a PER-PAPER, persisted choice (like the
    // review venue) — a researcher reads different papers in different
    // languages, so a global toggle would fight the user. Derived fresh from
    // runtimeScope._activePaperId on every call (no module global to reset on paper switch);
    // defaults to the current UI language on first open. The report is really
    // generated + cached per (paper_hash, lang), so switching regenerates.
    uiLang: function() { return runtimeScope._activeReportLang(); },
    langKey: function() { return this.uiLang(); },
    get cache() { return _paperReportCache; }, set cache(v) { _paperReportCache = v; },
    get meta() { return _paperReportMeta; }, set meta(v) { _paperReportMeta = v; },
    get stream() { return _paperReportStream; }, set stream(v) { _paperReportStream = v; },
    get model() { return _paperReportModel; }, set model(v) { _paperReportModel = v; },
  };
}

/**
 * Apply a server-resolved paper title to the active paper, live.
 *
 * The report backend self-heals library rows whose title is still a bare
 * `arXiv:<id>` (the up-front arXiv lookup failed) by extracting the real
 * title from the report's Paper Card and upserting it. It returns that title
 * in the report `done` / cache responses as `resolvedTitle`. We apply it to
 * the in-memory entry + sidebar immediately so the user never has to reload.
 *
 * Guards (mirror the backend): only overwrite when the current local title is
 * empty or itself a bare `arXiv:<id>` — never clobber a user-renamed title.
 * Scoped to the paper the resolution belongs to (paperId), so a background
 * stream for paper A can't rename the now-active paper B.
 */
function _applyResolvedTitle(resolvedTitle, paperId) {
  var title = (resolvedTitle || '').trim();
  if (!title) return;
  var pid = paperId || runtimeScope._activePaperId;
  var entry = null;
  for (var i = 0; i < runtimeScope._paperLibrary.length; i++) {
    if (runtimeScope._paperLibrary[i].id === pid) { entry = runtimeScope._paperLibrary[i]; break; }
  }
  if (!entry) return;
  var cur = (entry.title || '').trim();
  var isPlaceholder = !cur || /^arxiv[:\s]/i.test(cur);
  if (!isPlaceholder) return;       // respect a real / user-set title
  if (cur === title) return;        // no change
  entry.title = title;
  if (pid === runtimeScope._activePaperId) {
    _paperFileName = title;
    _updatePaperTitles();
  }
  runtimeScope._renderPaperLibrary();
  // Server healed the durable title; sync only lightweight client metadata.
  if (pid === runtimeScope._activePaperId) runtimeScope._saveActivePaperState('metadata');
}

function _updatePaperTitles() {
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  var noPaper = _tt('paper.noPaperOpen');
  var name = _paperFileName || noPaper;
  var stitle = document.getElementById('paperSidebarTitle');
  if (stitle) {
    stitle.textContent = name;
    stitle.title = name;
    stitle.classList.toggle('paper-sidebar-title-empty', !_paperFileName);
  }
  var pageCount = document.getElementById('paperPageCount');
  if (pageCount && _paperTotalPages) {
    pageCount.textContent = _tt('paper.pages', { count: _paperTotalPages });
  } else if (pageCount) {
    pageCount.textContent = '';
  }
  // Topbar title reflects Paper Mode, not the previous conversation
  if (paperMode) {
    var topbar = document.getElementById('topbarTitle');
    if (topbar) {
      var label = _paperFileName ? _paperFileName : _tt('paper.title');
      topbar.textContent = label;
      topbar.title = label;
    }
  }
}

// ══════════════════════════════════════════════════════
//  Landing / Upload Screen
// ══════════════════════════════════════════════════════

function _showPaperLanding() {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  viewer.innerHTML =
    '<div class="paper-landing">' +
      '<div class="paper-landing-icon">' + Icon('file', 40) + '</div>' +
      '<h3>' + escapeHtml(_tt('paper.title')) + '</h3>' +
      '<p>' + escapeHtml(_tt('paper.landingDesc')) + '</p>' +
      '<div class="paper-landing-actions">' +
        '<label class="paper-upload-btn">' +
          '<input type="file" accept=".pdf,application/pdf" data-tofu-action-change="_handlePaperFileUpload(event)" style="display:none">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
          ' ' + escapeHtml(_tt('paper.uploadPdf')) +
        '</label>' +
        '<div class="paper-arxiv-input">' +
          '<svg class="paper-arxiv-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
          '<input type="text" id="paperArxivUrl" placeholder="' + escapeHtml(_tt('paper.arxivPlaceholder')) + '"' +
                 ' data-tofu-action-keydown="if(event.key===\'Enter\')_submitArxivQuery()">' +
          '<button data-tofu-action="_submitArxivQuery()" class="paper-arxiv-btn">' + escapeHtml(_tt('paper.search')) + '</button>' +
        '</div>' +
        '<div class="paper-describe-divider"><span>' + escapeHtml(_tt('paper.describeOr')) + '</span></div>' +
        '<div class="paper-describe-box">' +
          '<div class="paper-describe-label">' +
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2Z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>' +
            ' ' + escapeHtml(_tt('paper.describeLabel')) +
          '</div>' +
          '<textarea id="paperDescribeInput" class="paper-describe-input" rows="3" placeholder="' + escapeHtml(_tt('paper.describePlaceholder')) + '"' +
                 ' data-tofu-action-input="_setPaperDescribeDraft(this)"' +
                 ' data-tofu-action-keydown="if(event.key===\'Enter\'&&(event.metaKey||event.ctrlKey))_submitPaperDescribe()">' + escapeHtml(_paperDescribeDraft) + '</textarea>' +
          '<button data-tofu-action="_submitPaperDescribe()" class="paper-describe-btn">' +
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>' +
            ' ' + escapeHtml(_tt('paper.describeBtn')) +
          '</button>' +
        '</div>' +
      '</div>' +
    '</div>';
}

function _showPaperLandingForNew() {
  // Clear in-memory "which paper am I looking at" state and show the landing.
  // No new DB row is created until the user actually uploads or fetches a PDF.
  runtimeScope._setActivePaperId('');
  _paperPdfUrl = '';
  _paperPdfFilename = '';
  _paperFileName = '';
  _paperParsedText = '';
  _paperArxivId = '';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperReviewCache = '';
  _paperReviewVenue = '';
  _paperHash = '';
  _paperImages = [];
  runtimeScope._babelTranslatedPages = {};
  _paperTotalPages = 0;
  _updatePaperTitles();
  runtimeScope._renderPaperLibrary();
  _showPaperLanding();
}

async function _handlePaperFileDrop(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') return;
  if (!paperMode) {
    var enter = runtimeScope.enterPaperMode;
    if (typeof enter !== 'function') throw new Error('Paper session owner is unavailable');
    await enter();
  }
  await _paperUploadFile(file);
}

async function _handlePaperFileUpload(event) {
  var file = event.target.files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) return;
  await _paperUploadFile(file);
}

async function _paperUploadFile(file) {
  _paperLoading = true;

  // These behaviors moved to typed Paper owners. Resolve them through the
  // private runtime service table; bare identifiers would target the removed
  // classic concatenation scope and fail only after a user selected a PDF.
  var createEntry = runtimeScope._createPaperEntry;
  var renderLibrary = runtimeScope._renderPaperLibrary;
  var loadPdf = runtimeScope._loadPaperPdf;
  var savePaper = runtimeScope._saveActivePaperState;
  if (typeof createEntry !== 'function' || typeof renderLibrary !== 'function' ||
      typeof loadPdf !== 'function' || typeof savePaper !== 'function') {
    _paperLoading = false;
    throw new Error('Paper runtime owners are unavailable');
  }

  // Create a new library entry for this paper (_createPaperEntry sets runtimeScope._activePaperId).
  // Its id is sent WITH the upload so the server persists the bookshelf row
  // itself (server-authoritative ingest) — the paper survives even if this
  // tab closes before the client save. On failure we remove this entry so a
  // failed upload never leaves a ghost row.
  var _uploadEntry = createEntry(file.name);
  var _uploadPaperId = _uploadEntry.id;
  _paperFileName = file.name;
  _paperParsedText = '';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperReviewCache = '';
  _paperReviewVenue = '';
  _paperHash = '';
  _paperPdfFilename = '';
  _paperImages = [];
  runtimeScope._babelTranslatedPages = {};
  runtimeScope._paperFileName = _paperFileName;
  runtimeScope._paperParsedText = _paperParsedText;
  runtimeScope._paperQAHistory = _paperQAHistory;
  runtimeScope._paperReportCache = _paperReportCache;
  runtimeScope._paperReviewCache = _paperReviewCache;
  runtimeScope._paperReviewVenue = _paperReviewVenue;
  runtimeScope._paperHash = _paperHash;
  runtimeScope._paperPdfFilename = _paperPdfFilename;
  runtimeScope._paperImages = _paperImages;
  runtimeScope._paperTotalPages = 0;
  _updatePaperTitles();
  renderLibrary();

  var viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Uploading & parsing PDF…</div></div>';

  try {
    // Single round-trip: server saves the PDF, parses text, AND extracts
    // figures synchronously — no client-side parse fallback, no race with
    // a background image-extraction call.
    var formData = new FormData();
    formData.append('file', file);
    formData.append('paper_id', _uploadPaperId);
    var uploadData = await Api.paper.upload(formData);
    if (!uploadData || !uploadData.ok) throw new Error((uploadData && uploadData.error) || 'Upload failed');

    _paperPdfUrl = apiUrl(uploadData.pdf_url);
    _paperPdfFilename = uploadData.filename || '';
    _paperParsedText = uploadData.parsed_text || '';
    _paperHash = uploadData.paper_hash || '';
    _paperImages = Array.isArray(uploadData.images) ? uploadData.images : [];
    _paperTotalPages = uploadData.total_pages || 0;
    runtimeScope._paperPdfUrl = _paperPdfUrl;
    runtimeScope._paperPdfFilename = _paperPdfFilename;
    runtimeScope._paperParsedText = _paperParsedText;
    runtimeScope._paperHash = _paperHash;
    runtimeScope._paperImages = _paperImages;
    runtimeScope._paperTotalPages = _paperTotalPages;

    if (uploadData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + uploadData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('Paper parsed: ' + _paperTotalPages + ' pages, ' +
               (uploadData.text_length || _paperParsedText.length) + ' chars' +
               (_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
               'success');
    }

    _updatePaperTitles();
    await loadPdf(_paperPdfUrl);
    // The server already persisted the row at ingest; await this so an
    // immediate exit/refresh can't race the first save (beforeunload is a
    // backstop only). Marks the entry _persisted so later saves are incremental.
    await savePaper();

  } catch (e) {
    console.error('[Paper] Upload failed:', e);
    // A failed upload must NOT leave a ghost bookshelf entry (the qa=0/imgs=0
    // rows behind the vanishing-paper report). Drop the entry we optimistically
    // created; the server never persisted a row for a failed save.
    var library = Array.isArray(runtimeScope._paperLibrary)
      ? runtimeScope._paperLibrary : [];
    runtimeScope._paperLibrary = library.filter(function(p) {
      return p.id !== _uploadPaperId;
    });
    if (runtimeScope._activePaperId === _uploadPaperId &&
        typeof runtimeScope._setActivePaperId === 'function') {
      runtimeScope._setActivePaperId('');
    }
    renderLibrary();
    if (viewer) viewer.innerHTML = '<div class="paper-error">Upload failed: ' + escapeHtml(e.message) + '</div>';
  } finally {
    _paperLoading = false;
  }
}

/** Format bytes as a human-friendly string (KB / MB). */
function _formatPaperBytes(n) {
  if (!n || n < 0) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(2) + ' MB';
}

/** Render the arXiv fetch progress UI into the PDF viewer. */
function _renderArxivFetchProgress(state) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var isZh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var labels = isZh
    ? { resolving: '解析 arXiv 链接…', downloading: '下载 PDF…',
        parsing: '解析 PDF 文本…', parsingImages: '提取图表…',
        pageOf: '第 {done} / {total} 页',
        cached: '已从缓存加载', pages: '页', chars: '字符' }
    : { resolving: 'Resolving arXiv link…', downloading: 'Downloading PDF…',
        parsing: 'Extracting PDF text…', parsingImages: 'Extracting figures…',
        pageOf: 'page {done} / {total}',
        cached: 'Loaded from cache', pages: 'pages', chars: 'chars' };

  var title;
  if (state.stage === 'resolve') title = labels.resolving;
  else if (state.stage === 'download') title = labels.downloading;
  else if (state.stage === 'download_done') title = state.cached ? labels.cached : labels.downloading;
  else if (state.stage === 'parse_start' || state.stage === 'parse_done') title = labels.parsing;
  else if (state.stage === 'parse_progress') {
    title = (state.parse_stage === 'images') ? labels.parsingImages : labels.parsing;
  }
  else title = labels.resolving;

  var pct = 0;
  var detail = '';
  if (state.stage === 'download') {
    if (state.total > 0) {
      pct = Math.min(100, Math.round(state.downloaded * 100 / state.total));
      detail = _formatPaperBytes(state.downloaded) + ' / ' + _formatPaperBytes(state.total);
    } else {
      detail = _formatPaperBytes(state.downloaded);
      pct = -1;  // indeterminate
    }
  } else if (state.stage === 'download_done') {
    pct = 100;
    detail = _formatPaperBytes(state.file_size || 0);
  } else if (state.stage === 'parse_start') {
    pct = -1;
    detail = '';
  } else if (state.stage === 'parse_progress') {
    var done = state.page || 0;
    var total = state.total_pages || 0;
    if (total > 0) {
      pct = Math.min(100, Math.round(done * 100 / total));
      detail = labels.pageOf.replace('{done}', done).replace('{total}', total);
    } else {
      pct = -1;
      detail = '';
    }
  } else if (state.stage === 'parse_done') {
    pct = 100;
    detail = (state.total_pages || 0) + ' ' + labels.pages +
             ' · ' + (state.text_length || 0).toLocaleString() + ' ' + labels.chars;
  }

  var barStyle = (pct < 0)
    ? 'width:40%;animation:paperProgressIndet 1.2s ease-in-out infinite'
    : 'width:' + pct + '%';

  viewer.innerHTML =
    '<div class="paper-loading paper-fetch-progress">' +
      '<div class="paper-loading-spinner"></div>' +
      '<div class="paper-fetch-title">' + escapeHtml(title) +
        (state.arxiv_id ? ' <span class="paper-fetch-id">arXiv:' + escapeHtml(state.arxiv_id) + '</span>' : '') +
      '</div>' +
      '<div class="paper-fetch-bar-wrap"><div class="paper-fetch-bar" style="' + barStyle + '"></div></div>' +
      (detail ? '<div class="paper-fetch-detail">' + escapeHtml(detail) + '</div>' : '') +
    '</div>';
}
runtimeScope._renderArxivFetchProgress = _renderArxivFetchProgress;

/** Recover paper text by asking the server to re-parse the already-stored PDF.
 * Used when a library entry was saved before server-side parsing (or parsing failed).
 * Returns true on success, false otherwise. */
async function _ensurePaperText() {
  if (_paperParsedText) return true;
  // Figure out the server filename — prefer the stored one, fall back to URL match
  var fname = _paperPdfFilename;
  if (!fname && _paperPdfUrl) {
    var m = /\/api\/paper\/pdf\/([^?#]+)/.exec(_paperPdfUrl);
    if (m) fname = decodeURIComponent(m[1]);
  }
  if (!fname) return false;
  try {
    debugLog('[Paper] Re-parsing PDF to recover text…', 'info');
    var data = await Api.paper.reparse(fname);
    if (!data || !data.ok || !data.text) {
      debugLog('[Paper] Re-parse failed: ' + (data.error || 'empty text'), 'warning');
      return false;
    }
    _paperParsedText = data.text;
    if (data.total_pages) _paperTotalPages = data.total_pages;
    runtimeScope._saveActivePaperState('metadata');
    debugLog('[Paper] Recovered ' + (data.text_length || data.text.length) + ' chars from PDF', 'success');
    return true;
  } catch (e) {
    console.warn('[Paper] Re-parse request failed:', e);
    debugLog('[Paper] Re-parse request failed: ' + (e.message || e), 'warning');
    return false;
  }
}

// ══════════════════════════════════════════════════════
//  Keyboard Shortcuts
// ══════════════════════════════════════════════════════

function _handlePaperKeyDown(e) {
  if (!paperMode) return;
  if (e.key === 'Escape') { e.preventDefault(); runtimeScope.exitPaperMode(); return; }
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    if (e.key === 'Enter' && !e.shiftKey && e.target.id === 'paperQAInput') { e.preventDefault(); runtimeScope._sendPaperQuestion(); }
    return;
  }
  if (e.key === '+' || e.key === '=') { runtimeScope.paperZoomIn(); e.preventDefault(); }
  if (e.key === '-') { runtimeScope.paperZoomOut(); e.preventDefault(); }
  if (e.key === '0') { runtimeScope.paperFitWidth(); e.preventDefault(); }
}

// ══════════════════════════════════════════════════════
//  Init
// ══════════════════════════════════════════════════════


/* Explicit renderer surface consumed by the native Paper owners. */
if (typeof window !== 'undefined') {
  runtimeScope._reportView = _reportView;
  runtimeScope._applyResolvedTitle = _applyResolvedTitle;
  runtimeScope._ensurePaperText = _ensurePaperText;
  runtimeScope._restoreRebuttalPanel = _restoreRebuttalPanel;
  runtimeScope._syncReviewSegState = _syncReviewSegState;
  runtimeScope._updatePaperTitles = _updatePaperTitles;
  runtimeScope._showPaperLanding = _showPaperLanding;
  runtimeScope._handlePaperKeyDown = _handlePaperKeyDown;
  runtimeScope._handlePaperFileDrop = _handlePaperFileDrop;
  runtimeScope._handlePaperFileUpload = _handlePaperFileUpload;
  runtimeScope._setPaperDescribeDraft = _setPaperDescribeDraft;
  runtimeScope._showPaperLandingForNew = _showPaperLandingForNew;
}

// BEGIN GENERATED LAZY RUNTIME PORTS — paper-reader-presenters
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — paper-reader-presenters
runtimeScope._autofillOpenReview = _autofillOpenReview;
runtimeScope._copyPaperRebuttal = _copyPaperRebuttal;
runtimeScope._copyPaperReport = _copyPaperReport;
runtimeScope._copyPaperReview = _copyPaperReview;
runtimeScope._exportPaperReport = _exportPaperReport;
runtimeScope._exportPaperReview = _exportPaperReview;
runtimeScope._handlePaperFileUpload = _handlePaperFileUpload;
runtimeScope._scrollReportToHeading = _scrollReportToHeading;
runtimeScope._setPaperDescribeDraft = _setPaperDescribeDraft;
runtimeScope._showPaperLanding = _showPaperLanding;
runtimeScope._showPaperLandingForNew = _showPaperLandingForNew;
runtimeScope._togglePaperReportExportMenu = _togglePaperReportExportMenu;
runtimeScope._togglePaperReportModelDropdown = _togglePaperReportModelDropdown;
runtimeScope._togglePaperReviewExportMenu = _togglePaperReviewExportMenu;
runtimeScope._togglePaperReviewModelDropdown = _togglePaperReviewModelDropdown;
runtimeScope._toggleReviewVenueDropdown = _toggleReviewVenueDropdown;
// END GENERATED LAZY RUNTIME ACTIONS
