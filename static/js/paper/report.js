/* ═════════════════════════════════════════════════
   paper/report.js — Report + Review Mode (server-owned task + poll + render).

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #6 — the biggest, ~2400 L). The Tab-2 Report AND Review-Mode stack:
   start/poll/apply/paint (parameterized by _reportView `kind`), regen-intent,
   snapshot cache, model+venue dropdowns, glossary cards, zoomable figures,
   export. Plus the 7 load-time document.addEventListener wirings (glossary
   hover, dropdown-close, figure zoom click/keydown, venue-close) — all only
   CALL document.addEventListener at load; their bodies run at runtime.
   ALL report/review STATE (_paperReportCache/_paperReviewCache/_paperReportStream
   /_paperReportSnapshots/_paperReportMeta/_paperReportModel/_paperImages/_paperHash)
   STAYS in the core State block (read across many clusters). Only functions +
   listeners move. Window-scope var + runtime cross-refs → loads before paper-reader.js.
   ═════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ Tab 2: Report — server-owned background task + polling
// ══════════════════════════════════════════════════════
//
// ARCHITECTURE (2026-04-18 rewrite)
//   • Reports are generated EXACTLY ONCE on the server per (paper_hash, lang).
//     On completion the enriched report is persisted to `paper_reports`.
//   • The frontend is purely a progress renderer. It never owns report state.
//   • Flow:
//       POST /api/paper/report/start  → {task_id} (or {cached, report} if DB hit)
//       GET  /api/paper/report/poll?task_id=X&cursor=N → {events, next_cursor, …}
//   • On tab/mode switch, we simply pause the poll timer. On return, we
//     lookup the task by paper_hash via /api/paper/report/lookup and resume
//     polling from cursor=0, replaying all events → UI rebuilt from events.
//   • Tool-round events (tool_start / tool_done) use the same schema as
//     chat tool events, so `renderToolRoundsHTML(toolRounds)` from ui.js
//     renders them identically to how they look in the chat bubble.

/** Persist a "regenerate in progress for (paperHash, lang)" intent to
 *  localStorage. Written synchronously the instant the user clicks Regenerate
 *  — BEFORE the force /start network round-trip — so a refresh that lands in
 *  that sub-second window still knows to resume the regenerate (re-issuing
 *  force /start) instead of falling through to the stale DB-cached report.
 *  Mirrors the chat edit-resend "persist truncation before the atomic call"
 *  fix. A null/empty hash clears the intent. */
function _setReportRegenIntent(paperHash, lang, key) {
  key = key || _REPORT_REGEN_INTENT_KEY;
  try {
    if (!paperHash) { localStorage.removeItem(key); return; }
    localStorage.setItem(key,
      JSON.stringify({ paperHash: paperHash, lang: lang || 'en', ts: Date.now() }));
  } catch (e) {
    console.warn('[Paper:Report] persist regen intent failed:', e);
  }
}

/** Read the pending regenerate intent, or null. */
function _getReportRegenIntent(key) {
  try {
    var raw = localStorage.getItem(key || _REPORT_REGEN_INTENT_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    console.warn('[Paper:Report] read regen intent failed:', e);
    return null;
  }
}

/** Clear the regenerate intent once it has been fulfilled (a task was
 *  attached) or reached a terminal state. Re-entrancy guard against a
 *  refresh→infinite-regenerate loop: the moment we attach to a task_id the
 *  intent is considered honoured and removed. */
function _clearReportRegenIntent(key) {
  try { localStorage.removeItem(key || _REPORT_REGEN_INTENT_KEY); }
  catch (e) { console.warn('[Paper:Report] clear regen intent failed:', e); }
}

/** True when a pending regenerate intent matches the active paper + lang. */
function _hasReportRegenIntent(paperHash, lang, key) {
  var it = _getReportRegenIntent(key);
  return !!(it && it.paperHash && it.paperHash === paperHash
            && (it.lang || 'en') === (lang || 'en'));
}

/** Reset a view's stream state (called when switching paper / force regen).
 *  `view` defaults to the report view — the historical behaviour. */
function _resetReportLocalState(view) {
  view = view || _reportView('report');
  if (view.stream && view.stream.pollTimer) {
    clearTimeout(view.stream.pollTimer);
  }
  view.stream = null;
  view.meta = null;  // drop stale finish tag from the previous paper/run
  // Review: drop the per-paper translated reading view so it never leaks
  // across papers (the English review is reloaded from server cache on demand).
  if (view.kind === 'review') {
    _paperReviewShowTranslation = false;
    _paperReviewTranslatedText = '';
    _paperReviewTranslating = false;
  }
  // Flush the current reading session into the learning model (the report
  // we were reading is going away — switching paper / regenerating).
  if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
}

/** Reset BOTH report + review view state. Used on paper switch / mode exit so
 *  neither view leaks across papers. */
function _resetAllReportViews() {
  _resetReportSnapshots();  // session snapshots are per-paper; drop on switch
  _resetReportLocalState(_reportView('report'));
  _resetReportLocalState(_reportView('review'));
  _resetReportLocalState(_reportView('rebuttal'));
  // The rebuttal paste is per-paper (persisted by id); drop the in-memory copy
  // so paper B never inherits paper A's pasted rebuttal before rehydration.
  _paperRebuttalInputText = '';
}

function _makeReportStreamState(paperId, lang, taskId, kind) {
  return {
    paperId: paperId || '',
    lang: lang || 'en',
    kind: kind || 'report',
    taskId: taskId || '',
    cursor: 0,
    status: 'running',
    // Set when the user presses Stop BEFORE the /start round-trip has returned
    // a task_id (start can take 10–40s). Honoured the moment the task_id lands
    // so a Stop in that window is not silently dropped.
    pendingStop: false,
    // Set the instant Stop is pressed (with or without a task_id). Keeps the
    // Stop button disabled + "Stopping…" across poll repaints while the task is
    // still `running`, so it can't be clicked again before the server's
    // authoritative `aborted` event lands.
    stopRequested: false,
    fullText: '',
    thinkingText: '',
    toolRounds: [],      // chat-compatible: [{roundNum, toolName, query, toolCallId, toolArgs, status, toolContent, _elapsed}]
    contentStarted: false,
    insightText: '',       // gated insight second-pass section (appended after `done`)
    _insightRunning: false,
    _insightApplied: false,
    termfillText: '',      // gated definition-backfill addendum (appended after `done`)
    _termfillApplied: false,
    meta: null,          // finish-tag {model, costCny, ...} from the done event
    error: '',
    pollTimer: null,
    pollBusy: false,
    _lastRenderedLen: -1,
    _lastRenderedStatus: '',
    _lastToolKey: '',
  };
}

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

/** Apply a single event to the in-memory stream state. Returns dirty flag. */
function _applyReportEvent(s, ev) {
  switch (ev.type) {
    case 'status':
      s.status = ev.status || s.status;
      return true;

    case 'thinking':
      s.thinkingText += (ev.delta || '');
      return true;

    case 'tool_start': {
      // Chat-compatible round entry
      s.toolRounds.push({
        roundNum: ev.roundNum,
        toolName: ev.toolName,
        query: ev.query || ev.toolName,
        toolCallId: ev.toolCallId || '',
        toolArgs: ev.toolArgs || '',
        status: 'searching',
        results: null,
      });
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
      }
      return true;
    }

    case 'tool_progress': {
      var rp = null;
      for (var j = 0; j < s.toolRounds.length; j++) {
        if (s.toolRounds[j].roundNum === ev.roundNum) { rp = s.toolRounds[j]; break; }
      }
      if (rp) {
        if (typeof rp._partialOutput !== 'string') rp._partialOutput = '';
        rp._partialOutput += (ev.chunk || '');
      }
      return true;
    }

    case 'delta':
      s.fullText += (ev.delta || '');
      s.contentStarted = true;
      return true;

    case 'delta_reset':
      // The model emitted an interim draft alongside a tool call; the
      // backend discards it and will rewrite the full report after the tool
      // results land. Clear the accumulated text so the draft + final report
      // don't concatenate (report rendered twice).
      s.fullText = '';
      s.contentStarted = false;
      s._lastRenderedLen = -1;
      return true;

    case 'enriched':
      s.fullText = ev.text || s.fullText;
      // Only mutate global hash when this stream still belongs to the active paper —
      // a stream that was started for paper A and is now polling in the background
      // must not stomp paper B's hash.
      if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
      return true;

    case 'insight_start':
      // The gated insight second-pass has begun (after the report `done`).
      // Flag it so the reader can show a subtle "synthesizing insight…" hint;
      // no body change yet.
      s._insightRunning = true;
      return true;

    case 'insight': {
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
        if (s.paperId === _activePaperId) {
          var _vIns = _reportView(s.kind);
          if (_vIns.cache) {
            _vIns.cache = _vIns.cache.replace(/\s*$/, '') + '\n\n' + _ins + '\n';
            _rememberReportSnapshot(_vIns, _vIns.cache, _vIns.meta || s.meta);
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
        if (s.paperId === _activePaperId) {
          var _vTf = _reportView(s.kind);
          if (_vTf.meta && _vTf.meta.terminologyAudit) _vTf.meta.terminologyAudit = null;
          if (_vTf.cache) {
            _vTf.cache = _vTf.cache.replace(/\s*$/, '') + '\n\n' + _add + '\n';
            _rememberReportSnapshot(_vTf, _vTf.cache, _vTf.meta || s.meta);
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
        if (s.paperId === _activePaperId) {
          _vDone.cache = ev.report;
          _rememberReportSnapshot(_vDone, ev.report, ev.meta || s.meta);
          _persistGeneratedReviewVenue(_vDone, _vDone.langKey(), s.paperId);
        }
      }
      if (ev.meta) {
        s.meta = ev.meta;
        if (s.paperId === _activePaperId) _vDone.meta = ev.meta;
      }
      if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
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
      out.push({ term: term, def: defText, defHtml: defHtml });
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
      map[key] = { row: r, def: glossary[r].def, defHtml: glossary[r].defHtml };
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
      picks.push({ idx: idx, len: matched.length, text: matched, def: entry.def, defHtml: entry.defHtml });
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
      if (pk.defHtml) card.innerHTML = pk.defHtml;
      else card.textContent = pk.def;
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
      + '" onclick="_scrollReportToHeading(event,\'' + e.id + '\')">' + escapeHtml(e.text) + '</a></li>';
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
  var costStr = '';
  if (typeof meta.costCny === 'number' && meta.costCny > 0) {
    costStr = (typeof formatCny === 'function') ? formatCny(meta.costCny)
      : ('¥' + meta.costCny.toFixed(4));
  } else if (typeof meta.costUsd === 'number' && meta.costUsd > 0) {
    costStr = '$' + meta.costUsd.toFixed(4);
  }
  if (costStr) {
    parts.push('<span class="paper-finish-cost" title="' +
      escapeHtml(t('paper.finishCostTitle')) +
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
      _persistReadingPosition(tracker.view, _captureReadingAnchor(scroller));
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
    if (tk.scroller) _persistReadingPosition(tk.view, _captureReadingAnchor(tk.scroller));
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
  var _readAnchor = _captureReadingAnchor(container) || _loadReadingPosition(view);

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
    if (readBar) container.appendChild(readBar);
    container.appendChild(doc);
    _wireReportScrollSpy(container, article, doc.querySelector('.paper-report-toc'));
  } else {
    container.innerHTML = '';
    if (readBar) container.appendChild(readBar);
    container.appendChild(article);
  }
  // Restore the pre-repaint reading position (see _captureReadingAnchor).
  _restoreReadingAnchor(container, article, _readAnchor);
  if (readBar) _wireReadingTimeTracking(readBar, container, view);
}

/** Snapshot the reader's place in `scroller` as {index, offset}: the index of
 *  the heading (h2/h3) nearest the top of the viewport and that heading's pixel
 *  distance below the scroller's top edge. Returns null when nothing is
 *  scrolled yet (fresh render) so a first paint is not perturbed. */
function _captureReadingAnchor(scroller) {
  try {
    if (!scroller || scroller.scrollTop <= 2) return null;
    var heads = scroller.querySelectorAll('.paper-report-article h2, .paper-report-article h3');
    if (!heads.length) {
      // No headings — fall back to a scroll FRACTION (best-effort for prose
      // whose length differs across languages).
      var max = scroller.scrollHeight - scroller.clientHeight;
      return max > 0 ? { frac: scroller.scrollTop / max } : null;
    }
    var sTop = scroller.getBoundingClientRect().top;
    var best = 0, bestAbove = -Infinity;
    for (var i = 0; i < heads.length; i++) {
      var rel = heads[i].getBoundingClientRect().top - sTop;
      // The last heading at or above the top edge is the one we're "in".
      if (rel <= 1 && rel > bestAbove) { bestAbove = rel; best = i; }
    }
    var relTop = heads[best].getBoundingClientRect().top - sTop;
    return { index: best, offset: relTop };
  } catch (e) {
    console.debug('[Paper] captureReadingAnchor failed: %s', e && e.message);
    return null;
  }
}

/** Restore a {index,offset} (or {frac}) anchor produced by
 *  _captureReadingAnchor onto the freshly-rebuilt `article` inside `scroller`. */
function _restoreReadingAnchor(scroller, article, anchor) {
  if (!scroller || !anchor) return;
  try {
    if (anchor.frac != null) {
      var max = scroller.scrollHeight - scroller.clientHeight;
      scroller.scrollTop = Math.max(0, Math.round(anchor.frac * max));
      return;
    }
    var heads = article.querySelectorAll('h2, h3');
    if (!heads.length || anchor.index == null) return;
    var idx = Math.min(anchor.index, heads.length - 1);
    var sTop = scroller.getBoundingClientRect().top;
    var headTop = heads[idx].getBoundingClientRect().top - sTop + scroller.scrollTop;
    scroller.scrollTop = Math.max(0, Math.round(headTop - (anchor.offset || 0)));
  } catch (e) {
    console.debug('[Paper] restoreReadingAnchor failed: %s', e && e.message);
  }
}

/** Read the whole persisted reading-position map { '<paperId>::<langKey>':
 *  anchor }. Never throws. */
function _readReadPosMap() {
  try {
    var raw = localStorage.getItem(_PAPER_READ_POS_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper] read reading-position map failed:', e);
    return {};
  }
}

/** Persist a `_captureReadingAnchor` anchor for the view's current language so
 *  it survives a tab switch / reload. Keyed by the SAME composite key as the
 *  report snapshots (paper + langKey) so each language keeps its own place.
 *  A null/empty anchor CLEARS the slot (reader is back at the top). */
function _persistReadingPosition(view, anchor) {
  view = view || _reportView('report');
  if (!_activePaperId) return;
  var key = _reportSnapshotKey(view);
  try {
    var map = _readReadPosMap();
    if (anchor) map[key] = anchor;
    else delete map[key];
    localStorage.setItem(_PAPER_READ_POS_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper] persist reading-position failed:', e);
  }
}

/** The persisted anchor for the view's current language, or null. */
function _loadReadingPosition(view) {
  view = view || _reportView('report');
  if (!_activePaperId) return null;
  return _readReadPosMap()[_reportSnapshotKey(view)] || null;
}

/** Paint a view's tab DOM from its current stream state. `view` defaults to
 *  the report view (historical callers pass nothing). */
function _paintReportFromState(view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container || !view.stream) return;
  var s = view.stream;
  var px = view.idPrefix;
  var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';

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

  // Tool rounds — reuse chat's unified renderer for identical look & feel
  var toolZone = document.getElementById(px + 'ToolZone');
  if (toolZone) {
    var toolCount = s.toolRounds.length;
    var searchingCount = s.toolRounds.filter(r => r.status === 'searching').length;
    var toolKey = toolCount + ':' + searchingCount;
    if (s._lastToolKey !== toolKey) {
      if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
        toolZone.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
      } else {
        toolZone.innerHTML = '';
      }
      s._lastToolKey = toolKey;
    }
  }

  // Thinking
  if (s.thinkingText) {
    var thBlock = document.getElementById(px + 'ThinkingBlock');
    var thBody = document.getElementById(px + 'ThinkingBody');
    if (thBlock) {
      thBlock.style.display = '';
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
        '<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
    }
    // Otherwise keep the loading spinner from the skeleton
  }
}

/** Poll /api/paper/report/poll once; schedule next if still running.
 *  Parameterized by `view` so ONE poll loop drives both Report and Review
 *  tabs (shared engine/endpoints; only cache key, state slots, DOM differ). */
async function _pollReportTask(view) {
  view = view || _reportView('report');
  var s = view.stream;
  if (!s || !s.taskId) return;
  // Abandon guard: this poll chain belongs to stream `s`. If the view's active
  // stream has since been REPLACED (paper switch, force-regenerate, reset),
  // `s` is orphaned — stop this chain dead so it can neither repaint into a
  // dead stream nor schedule a DUPLICATE poll onto the new stream. This is the
  // root fix for the "clicked Regenerate / toggled segment a few times → the
  // button freezes" bug: without it, an in-flight poll captured on the old
  // stream resumes, sees its own stale status==='running', and stacks a second
  // poll chain (and racing repaints) on whatever stream is now active.
  if (view.stream !== s) return;
  if (s.pollBusy) return;
  s.pollBusy = true;
  try {
    var resp = await Api.paper.reportPoll(s.taskId, s.cursor);
    if (!resp || !resp.ok) {
      if (resp && resp.status === 404) {
        // Task expired or server restarted
        s.status = 'error';
        s.error = 'Task no longer available on server. Please regenerate.';
        _paintReportFromState(view);
        return;
      }
      throw new Error('HTTP ' + resp.status);
    }
    var data = await resp.json();
    if (!data.ok) {
      s.status = 'error';
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(data.error) : '')
                || (typeof data.error === 'string' ? data.error : '')
                || 'Poll failed';
      _paintReportFromState(view);
      return;
    }

    // Apply new events
    var events = data.events || [];
    for (var i = 0; i < events.length; i++) {
      _applyReportEvent(s, events[i]);
    }
    s.cursor = data.next_cursor;

    // Any terminal status means a regenerate (if one was pending) has been
    // honoured end-to-end — clear the intent. (The attach point already
    // cleared it; this is a defensive backstop in case attach was skipped.)
    if (data.status === 'done' || data.status === 'aborted' || data.status === 'error') {
      _clearReportRegenIntent(view.regenIntentKey);
    }

    // Update status from server authoritative status
    if (data.status === 'done') {
      s.status = 'done';
      if (data.report) {
        s.fullText = data.report;
        // Only persist into the global cache + library entry when this poll's
        // stream is still bound to the active paper. Otherwise we'd overwrite
        // a different paper's report (e.g. user regenerated paper A then
        // switched to paper B before the task finished).
        if (s.paperId === _activePaperId) {
          view.cache = data.report;
          if (data.meta) { s.meta = data.meta; view.meta = data.meta; }
          _rememberReportSnapshot(view, data.report, data.meta);
          _persistGeneratedReviewVenue(view, view.langKey(), s.paperId);
          _saveActivePaperState();
        }
      }
      if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, s.paperId);
    } else if (data.status === 'aborted') {
      s.status = 'aborted';
      if (typeof data.partial === 'string' && data.partial) {
        s.fullText = data.partial;
        s.contentStarted = true;
      }
    } else if (data.status === 'error') {
      s.status = 'error';
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(data.error) : '')
                || (typeof data.error === 'string' ? data.error : '')
                || s.error;
    }

    // Only repaint DOM when the user is actually on this paper (and this tab)
    if (s.paperId === _activePaperId) {
      _paintReportFromState(view);
    }

    // Schedule next poll if still running — but ONLY while this chain still
    // owns the view's active stream (see the abandon guard above). On any
    // terminal status, null the timer handle so the single-poll-chain
    // invariant (`!view.stream.pollTimer`) elsewhere reads true.
    if (s.status === 'running' && view.stream === s) {
      s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 1200);
    } else {
      s.pollTimer = null;
    }
  } catch (e) {
    console.warn('[Paper:Report] Poll failed:', e);
    // Transient network error — retry with backoff, only if this chain still
    // owns the active stream (otherwise a duplicate chain would be spawned).
    if (s && s.status === 'running' && view.stream === s) {
      s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 3000);
    }
  } finally {
    s.pollBusy = false;
  }
}

/** Start (or join) a server-side report task, begin polling. Parameterized by
 *  `view` so the Review tab reuses this verbatim (only cache key / state slots
 *  / DOM container differ). */
async function _generatePaperReport(force, view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container) return;

  // Snapshot which paper this generation is for. If the user switches paper
  // mid-await, every continuation below must bail — otherwise paper A's
  // task_id / report / hash leak into paper B's state. (See bug 2026-05-20.)
  var startPaperId = _activePaperId;

  // Review generation MUST resolve the venue BEFORE building the composite
  // cache key. An entry that reaches here with _paperReviewVenue==='' (e.g. the
  // Generate button, which — unlike the venue-race-guarded _switchPaperTab —
  // does NOT pre-resolve) would build langKey off the `|| 'generic'` fallback
  // and generate/persist under review:generic:… while a later reload resolves
  // the real venue → cache-key skew → the finished review re-prompts Generate.
  // Mirror the tab-switch resolve-then-generate guard so every generation entry
  // agrees with reload.
  if (view.kind === 'review') {
    try { await _resolveReviewVenue(); } catch (e) {
      console.warn('[Paper:Review] venue resolve before generate failed:', e);
    }
    if (_activePaperId !== startPaperId) return;
  }

  var langKey = view.langKey();
  var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';

  // Already polling a live task for this paper and not forcing → just paint
  if (!force && view.stream
      && view.stream.paperId === _activePaperId
      && view.stream.status === 'running') {
    _paintReportFromState(view);
    return;
  }

  // In-memory cache — instant path
  if (view.cache && !force) {
    _renderFinalReport(container, view.cache, undefined, view);
    // Reopen restore: re-apply a persisted Chinese reading view (translate-only,
    // never regenerates the English review).
    _restoreReviewReadingLang(view);
    return;
  }

  if (!_paperParsedText) {
    container.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
      '<div>Recovering paper text…</div></div>';
    var ok = await _ensurePaperText();
    if (_activePaperId !== startPaperId) return;
    if (!ok) {
      container.innerHTML =
        '<div class="paper-report-empty"><p>' + escapeHtml((typeof t === 'function') ? t('paper.reportNoText') : 'No paper text available.') + '</p>' +
        '<p style="opacity:0.6;font-size:12px;margin-top:6px">The PDF may be scanned/image-only, or parsing failed. Try re-uploading.</p></div>';
      return;
    }
  }

  var reportLang = view.uiLang();
  if (!view.model) _populatePaperReportModelDropdown(view);
  var reportModel = view.model || null;

  // Discard any prior stream state (force path or new paper path)
  if (force || (view.stream && view.stream.paperId !== _activePaperId)) {
    _resetReportLocalState(view);
  }

  _renderReportSkeleton(container, reportLang, view);

  // Show the Stop affordance IMMEDIATELY. The /start round-trip below does
  // synchronous server prep (image-manifest extraction, injection sanitize,
  // prompt build) that legitimately runs 10–40s before it returns a task_id.
  // Without a provisional running stream, only Regenerate shows during that
  // whole window — so a user who picked the wrong model has NO way to stop
  // (exactly the reported bug). A Stop pressed now sets `pendingStop`, honoured
  // the instant the task_id lands.
  view.stream = _makeReportStreamState(startPaperId, reportLang, '', view.kind);
  _syncReportToolbar(true, view);

  try {
    // Title fallback: paper_library may not have been upserted yet (the PUT
    // is fire-and-forget) so the server can't always look up the title from
    // paper_hash. Send the active entry's title (without `.pdf`) so the
    // backend can still prepend `# Title` even when the DB is empty.
    var entryNow = _getActivePaperEntry();
    var clientTitle = (entryNow && entryNow.title)
      || _paperFileName
      || (_paperPdfFilename || '').replace(/^\d+_/, '');
    if (clientTitle) clientTitle = String(clientTitle).replace(/\.pdf$/i, '').trim();

    // Images are loaded from the server-side manifest by paper_hash —
    // the client doesn't forward them. filename is a fallback path the
    // server can use if no manifest exists yet (rare). `lang` carries the
    // composite key for reviews (``review:<venue>:<uilang>``), opaquely.
    var _startBody = {
      paper_text: _paperParsedText,
      lang: langKey,
      model: reportModel,
      force: !!force,
      title: clientTitle || '',
      filename: _paperPdfFilename || '',
    };
    // Rebuttal view: ship the author's pasted rebuttal text. The server pairs
    // it with the stored review row (review:<venue>:<uilang>) for this paper.
    if (view.kind === 'rebuttal') _startBody.author_rebuttal = _paperRebuttalInputText || '';
    var data = await Api.paper.reportStart(_startBody);
    if (_activePaperId !== startPaperId) return;
    if (!data || !data.ok) throw new Error((data && data.error) || 'Start failed');

    // Did the user press Stop while the (slow) /start was in flight? The
    // provisional stream created above carries the intent forward.
    var stopWasPending = !!(view.stream && view.stream.pendingStop);

    // DB cache hit — done in one round-trip. Drop the provisional running
    // stream first so a later tab re-entry paints the cached report, not a
    // stuck empty skeleton left behind by the provisional (taskId-less) state.
    if (data.cached && data.report) {
      view.stream = null;
      view.cache = data.report;
      view.meta = data.meta || null;
      if (data.paper_hash) _paperHash = data.paper_hash;
      _rememberReportSnapshot(view, data.report, data.meta);
      _persistGeneratedReviewVenue(view, langKey, startPaperId);
      _saveActivePaperState();
      if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, startPaperId);
      _renderFinalReport(container, data.report, undefined, view);
      return;
    }

    // Task started (or joined) — begin polling from cursor 0 so we replay all
    if (data.paper_hash) _paperHash = data.paper_hash;
    // Re-entrancy guard: a task is now attached, so the regenerate intent is
    // fulfilled. Clear it so a refresh can't re-trigger an endless regenerate.
    _clearReportRegenIntent(view.regenIntentKey);
    view.stream = _makeReportStreamState(startPaperId, reportLang, data.task_id, view.kind);
    _syncReportToolbar(true, view);
    _pollReportTask(view);
    // Honour a Stop pressed before the task_id existed: now that we know the
    // id, abort the just-started task instead of silently dropping the intent.
    if (stopWasPending) {
      console.warn('[Paper:Report] Stop was pending during start — aborting task ' + data.task_id);
      _stopPaperReport(view);
    }

  } catch (e) {
    if (_activePaperId !== startPaperId) return;
    console.warn('[Paper:Report] start failed:', e);
    // Drop the provisional running stream so the error state is terminal — a
    // later tab re-entry must not resume-poll a task_id-less zombie stream.
    view.stream = null;
    // Reset the toolbar to a clean Regenerate-only state. Without this, a Stop
    // pressed during the (now-failed) start left the Stop button disabled with
    // the "Stopping…" label (via pendingStop) — the poll loop's terminal
    // repaint never runs on a start FAILURE, so the toolbar would stay stuck.
    _syncReportToolbar(false, view);
    container.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message) +
      '<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
  }
}

/** Review-Mode entry: identical pipeline, review view-context. */
async function _generatePaperReview(force) {
  return _generatePaperReport(force, _reportView('review'));
}

/** Rebuttal entry: generate a follow-up reply + score decision from the
 *  author's pasted rebuttal. Requires (a) a non-empty rebuttal text and (b) an
 *  already-generated review for this venue (the server pairs them). The paste
 *  box + guards live in _renderRebuttalPanel; this just kicks the shared pipe. */
async function _generatePaperRebuttal(force) {
  var text = (_paperRebuttalInputText || '').trim();
  if (!text) {
    if (typeof showToast === 'function') {
      showToast((typeof t === 'function') ? t('paper.rebuttalNeedText') : 'Paste the author rebuttal first');
    }
    return;
  }
  // A rebuttal needs the original review to exist. The in-memory review cache
  // OR a live/finished review stream both count; otherwise nudge the user.
  var rv = _reportView('review');
  if (!rv.cache && !(rv.stream && rv.stream.fullText)) {
    if (typeof showToast === 'function') {
      showToast((typeof t === 'function') ? t('paper.rebuttalNeedReview') : 'Generate the review first');
    }
    return;
  }
  return _generatePaperReport(force, _reportView('rebuttal'));
}

function _stopPaperRebuttal() { return _stopPaperReport(_reportView('rebuttal')); }
async function _regeneratePaperRebuttal() { return _regeneratePaperReport(_reportView('rebuttal')); }


/** The review reading language for the ACTIVE paper: persisted per-paper if
 *  present, else 'en' (English is always the canonical generated language). */
function _activeReviewLang() {
  if (!_activePaperId) return 'en';
  var stored = _readReviewLangMap()[_activePaperId];
  return (stored === 'zh') ? 'zh' : 'en';
}

/** Read the per-paper review-reading-language map { paperId: 'en'|'zh' }. */
function _readReviewLangMap() {
  try {
    var raw = localStorage.getItem(_PAPER_REVIEW_LANG_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper:Review] read lang map failed:', e);
    return {};
  }
}

/** Persist the review reading language for the active paper. */
function _persistReviewLang(paperId, lang) {
  if (!paperId || (lang !== 'en' && lang !== 'zh')) return;
  try {
    var map = _readReviewLangMap();
    map[paperId] = lang;
    localStorage.setItem(_PAPER_REVIEW_LANG_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper:Review] persist lang failed:', e);
  }
}

/** After the canonical ENGLISH review is (re)rendered on a REOPEN (page reload
 *  or paper switch — both wipe the in-memory translation state via
 *  _resetAllReportViews), restore the per-paper persisted READING language: if
 *  the user last read this paper's review in Chinese, re-apply the translated
 *  view.
 *
 *  This ONLY translates the already-rendered English review — on a warm cache
 *  it is instant, on a cold cache it kicks the Babel translate task. It NEVER
 *  regenerates the English review (no report/start) and is invoked ONLY from
 *  terminal English-render sites (cache hits), so it never fights an in-flight
 *  generation. */
function _restoreReviewReadingLang(view) {
  if (!view || view.kind !== 'review') return;
  if (!view.cache) return;                    // no English review to translate
  if (_activeReviewLang() !== 'zh') return;   // last read in English → nothing to do
  // _setReviewLang('zh') translates the current English cache (cache-hit →
  // instant); it never issues a review generation request.
  _setReviewLang('zh');
}

/** Set the review READING language (bidirectional, always available — NOT
 *  gated on the app UI language).
 *
 *  English is always the canonical generated/cached/exported review (the
 *  language the authors/AC read). Selecting '中' shows an on-demand translated
 *  reading view (produced by the Babel translate task under a DISTINCT
 *  composite key ``review:<venue>:zh`` so it never collides with the
 *  whole-paper Babel cache) and caches it client-side for instant re-toggle;
 *  selecting 'EN' restores the canonical English render. The choice is
 *  persisted per paper so re-opening the Review tab restores the last language. */
async function _setReviewLang(lang) {
  if (lang !== 'en' && lang !== 'zh') return;
  var view = _reportView('review');
  var container = document.getElementById(view.containerId);
  if (!container) return;
  var english = view.cache;
  if (_activePaperId) _persistReviewLang(_activePaperId, lang);

  // English → restore the canonical render.
  if (lang === 'en') {
    _paperReviewShowTranslation = false;
    if (english) _renderFinalReport(container, english, view.meta, view);
    _syncReviewTranslateBtn();
    return;
  }

  if (!english) { _syncReviewTranslateBtn(); return; }  // nothing generated yet

  // Chinese, already translated → instant.
  if (_paperReviewTranslatedText) {
    _paperReviewShowTranslation = true;
    _renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
    _syncReviewTranslateBtn();
    return;
  }

  if (_paperReviewTranslating) return;
  _paperReviewTranslating = true;
  _syncReviewTranslateBtn();

  // Distinct cache key: venue + target lang, so the translated review is
  // cached per (paper, venue, target-lang) and never collides with the Babel
  // whole-paper translation cache keyed on the bare lang. Target is always
  // 'zh' (English is canonical, so we only ever translate INTO Chinese).
  var trKey = 'review:' + (_paperReviewVenue || 'generic') + ':zh';
  var startPaperId = _activePaperId;

  try {
    // (1) Client-server cache hit → instant.
    if (_paperHash) {
      try {
        var cd = await Api.paper.translateCache(_paperHash, trKey);
        if (cd && cd.ok && cd.text) {
          if (_activePaperId !== startPaperId) return;
          _paperReviewTranslatedText = cd.text;
          _paperReviewShowTranslation = true;
          _renderFinalReport(container, cd.text, view.meta, view);
          return;
        }
      } catch (e) { console.warn('[Paper:Review] translate cache lookup failed:', e); }
    }

    // (2) Start (or join) the translate task on the ENGLISH review markdown.
    var startData = await Api.paper.translateStart({
      paper_text: english, lang: trKey, paper_hash: _paperHash || '',
    });
    if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'translate start failed');
    if (startData.cached && startData.text) {
      if (_activePaperId !== startPaperId) return;
      _paperReviewTranslatedText = startData.text;
      _paperReviewShowTranslation = true;
      _renderFinalReport(container, startData.text, view.meta, view);
      return;
    }
    if (startData.paper_hash) _paperHash = startData.paper_hash;
    var taskId = startData.task_id;
    if (!taskId) throw new Error('translate task returned no task_id');

    // (3) Poll to completion. Reuses the Babel event schema (chunk/done/error).
    var cursor = 0;
    var parts = [];
    while (true) {
      if (_activePaperId !== startPaperId) { try { await Api.paper.translateAbort(taskId); } catch (_) {} return; }
      var pollResp = await Api.paper.translatePoll(taskId, cursor);
      if (!pollResp || !pollResp.ok) throw new Error('poll HTTP ' + (pollResp ? pollResp.status : 'none'));
      var pollData = await pollResp.json();
      if (!pollData.ok) throw new Error(pollData.error || 'poll failed');
      cursor = pollData.next_cursor || cursor;
      var events = pollData.events || [];
      for (var ei = 0; ei < events.length; ei++) {
        var ev = events[ei];
        if (ev.type === 'chunk') {
          parts.push(ev.text || '');
        } else if (ev.type === 'done') {
          if (_activePaperId !== startPaperId) return;
          _paperReviewTranslatedText = ev.text || parts.join('\n\n');
          _paperReviewShowTranslation = true;
          _renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
          return;
        } else if (ev.type === 'error') {
          var m = (typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(ev.error)
            : (typeof ev.error === 'string' ? ev.error : '');
          throw new Error(m || 'translation failed');
        }
      }
      if (pollData.status === 'error') throw new Error('translation failed');
      if (pollData.status === 'done' && !events.length) return;
      await new Promise(function(r) { setTimeout(r, 700); });
    }
  } catch (e) {
    console.warn('[Paper:Review] translate failed:', e);
    if (typeof showToast === 'function') {
      showToast((typeof t === 'function') ? t('paper.reviewTranslateFailed') : 'Translation failed', 'error');
    }
  } finally {
    _paperReviewTranslating = false;
    _syncReviewTranslateBtn();
  }
}

/** Back-compat: flip the review reading language to the OTHER one. Retained so
 *  any external caller keeps working; the UI now uses the EN/中 segmented
 *  control (_setReviewLang) directly. */
function _toggleReviewTranslation() {
  return _setReviewLang(_paperReviewShowTranslation ? 'en' : 'zh');
}

/** Sync the review EN/中 segmented control: active option reflects the current
 *  reading language; the '中' option shows a spinner + is disabled while a
 *  translation is in flight. The control is ALWAYS available (both directions),
 *  independent of the app UI language — it's only disabled until a review
 *  exists to read. */
function _syncReviewTranslateBtn() {
  var wrap = document.getElementById('reviewLangToggle');
  if (!wrap) return;
  var view = _reportView('review');
  var hasReview = !!view.cache;
  var cur = _activeReviewLang();
  wrap.style.opacity = hasReview ? '' : '0.5';
  wrap.querySelectorAll('.paper-report-lang-opt').forEach(function(btn) {
    var isZh = btn.dataset.lang === 'zh';
    btn.classList.toggle('active', btn.dataset.lang === cur);
    // Disable interactions until a review exists; disable '中' while translating.
    btn.disabled = !hasReview || (isZh && _paperReviewTranslating);
    if (isZh) {
      btn.classList.toggle('loading', !!_paperReviewTranslating);
      btn.title = _paperReviewTranslating
        ? ((typeof t === 'function') ? t('paper.reviewTranslating') : 'Translating…')
        : ((typeof t === 'function') ? t('paper.reviewTranslateTitle') : 'Read in Chinese');
    }
  });
}

/** Called when the user opens the Report tab. Priority:
 *   1. Have stream state for active paper → paint + resume poll if running.
 *   2. Look up server-side running task by paper_hash → attach + poll.
 *   3. Try DB cache lookup.
 *   4. Start a new task.
 */
async function _loadOrGenerateReport(view) {
  view = view || _reportView('report');
  var reportLang = view.uiLang();
  var langKey = view.langKey();
  var startPaperId = _activePaperId;

  // (1) Existing local stream state for this paper
  if (view.stream && view.stream.paperId === _activePaperId) {
    _paintReportFromState(view);
    if (view.stream.status === 'running' && !view.stream.pollTimer) {
      _pollReportTask(view);
    } else if (view.stream.status === 'done') {
      // Terminal English render on tab re-entry — re-apply a persisted Chinese
      // reading view (translate-only; cached translation is instant, never
      // regenerates). Guarded to 'done' so it never fires mid-generation.
      _restoreReviewReadingLang(view);
    }
    return;
  }

  // (1.5) Pending regenerate intent — MUST take priority over the step-2
  // lookup-reconnect. The user clicked Regenerate and a refresh interrupted
  // the force /start before the new task registered. The OLD task is still
  // RUNNING (cooperative abort) and the dedup index (paper_hash, lang) still
  // points to it, so step-2's lookup would re-attach to exactly the task the
  // user asked to REPLACE — silently swallowing the regenerate. Re-issuing
  // force /start is the right move: it atomically aborts the old task AND
  // starts a fresh one. _generatePaperReport(true) clears the intent the
  // moment it attaches the new task_id (re-entrancy guard against a
  // refresh→endless-regenerate loop).
  if (_hasReportRegenIntent(_paperHash, reportLang, view.regenIntentKey)) {
    console.warn('[Paper:Report] pending regenerate intent for hash=' + _paperHash
                 + ' lang=' + reportLang + ' kind=' + view.kind
                 + ' — resuming force-start (priority over lookup-reconnect)');
    _generatePaperReport(true, view);
    return;
  }

  // (1.6) In-memory cache for this view → paint instantly, no round-trip. This
  // is the fast path when the report was already loaded once this session.
  if (view.cache) {
    var cEl = document.getElementById(view.containerId);
    if (cEl) _renderFinalReport(cEl, view.cache, undefined, view);
    _restoreReviewReadingLang(view);
    return;
  }

  // The lookup + cache round-trips below are async. Until they resolve, replace
  // the static "Generate" empty-state (baked into index.html) with a neutral
  // loading placeholder — otherwise a paper that ALREADY has a report flashes
  // the Generate button before the cache hit paints. If every path misses,
  // step 4 renders the real Generate prompt over this placeholder.
  var loadEl = document.getElementById(view.containerId);
  if (loadEl) {
    loadEl.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
      '<div>' + escapeHtml((typeof t === 'function') ? t('paper.loadingReport') : 'Loading…') + '</div></div>';
  }

  // (2) Server-side task lookup (survives chat-mode round-trips). Uses the
  // composite langKey so a review reconnects to the review task, not the
  // plain report.
  if (_paperHash) {
    try {
      var lookupData = await Api.paper.reportLookup(_paperHash, langKey);
      if (_activePaperId !== startPaperId) return;
      if (lookupData && lookupData.ok && lookupData.task_id
          && (lookupData.status === 'running' || lookupData.status === 'pending')) {
        // Attach to the running server-side task
        var container = document.getElementById(view.containerId);
        if (container) _renderReportSkeleton(container, reportLang, view);
        view.stream = _makeReportStreamState(startPaperId, reportLang, lookupData.task_id, view.kind);
        _syncReportToolbar(true, view);
        _pollReportTask(view);
        return;
      }
    } catch (e) {
      if (_activePaperId !== startPaperId) return;
      console.warn('[Paper:Report] lookup failed (non-fatal):', e);
    }
  }

  // (3) Try server DB cache by hash (avoids re-sending text)
  try {
    var cacheBody = { lang: langKey };
    if (_paperHash) cacheBody.paper_hash = _paperHash;
    else cacheBody.paper_text = _paperParsedText;
    var cacheData = await Api.paper.reportCache(cacheBody);
    if (_activePaperId !== startPaperId) return;
    if (cacheData && cacheData.ok && cacheData.report) {
      view.cache = cacheData.report;
      view.meta = cacheData.meta || null;
      if (cacheData.paper_hash) _paperHash = cacheData.paper_hash;
      _rememberReportSnapshot(view, cacheData.report, cacheData.meta);
      _persistGeneratedReviewVenue(view, langKey, startPaperId);
      _saveActivePaperState();
      var c2 = document.getElementById(view.containerId);
      if (c2) _renderFinalReport(c2, cacheData.report, undefined, view);
      // Reopen restore: if the user last read THIS review in Chinese, re-apply
      // the translated view (translate-only, never regenerates the English).
      _restoreReviewReadingLang(view);
      return;
    }
  } catch (e) {
    if (_activePaperId !== startPaperId) return;
    console.warn('[Paper:Report] Cache lookup failed:', e);
  }

  // (3.5) The active language has no report — but the OTHER language may
  // already be generated. Per the "show whatever exists, only offer the manual
  // trigger when NOTHING exists" rule: probe the other report language and, if
  // it has a persisted report, adopt that language and paint it instead of the
  // Generate prompt. Report-only — a review is always English-canonical (its
  // Chinese view is a translate-only reading of the same English body, so there
  // is no per-language generated variant to fall back to).
  if (view.kind === 'report' && _paperHash) {
    var otherLang = (reportLang === 'zh') ? 'en' : 'zh';
    try {
      var otherData = await Api.paper.reportCache({ lang: otherLang, paper_hash: _paperHash });
      if (_activePaperId !== startPaperId) return;
      if (otherData && otherData.ok && otherData.report) {
        // Adopt the generated language as the active report language so the
        // toggle, snapshot key, and render all resolve to it consistently.
        _persistReportLang(_activePaperId, otherLang);
        _syncReportLangToggle(view);
        view.cache = otherData.report;
        view.meta = otherData.meta || null;
        if (otherData.paper_hash) _paperHash = otherData.paper_hash;
        _rememberReportSnapshot(view, otherData.report, otherData.meta);
        _saveActivePaperState();
        var c3 = document.getElementById(view.containerId);
        if (c3) _renderFinalReport(c3, otherData.report, undefined, view);
        return;
      }
    } catch (e) {
      if (_activePaperId !== startPaperId) return;
      console.warn('[Paper:Report] other-language cache lookup failed (non-fatal):', e);
    }
  }

  // (4) No cache in EITHER language, no running task — DO NOT auto-start. Show
  // a manual-start prompt so the user can first adjust the model / language /
  // venue in the toolbar, then click Generate. (User preference: never
  // auto-generate on tab open, otherwise the settings can't be tuned before
  // the run begins.)
  if (_activePaperId !== startPaperId) return;
  _renderReportStartPrompt(view);
}

/** Manual-start prompt: rendered when a paper is loaded but no report/review
 *  exists yet (no cache, no running task). The toolbar (model / language /
 *  venue) is already visible and tunable; the user clicks Generate to begin.
 *  This is the seam that keeps generation user-initiated. */
function _renderReportStartPrompt(view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container) return;
  _syncReportToolbar(false, view);
  var isReview = view.kind === 'review';
  var genFn = isReview ? '_generatePaperReview()' : '_generatePaperReport()';
  var _tt = (typeof t === 'function') ? t : function(k) { return k; };
  var title = _tt(isReview ? 'paper.reviewEmptyTitle' : 'paper.reportEmptyTitle');
  var hint = _tt(isReview ? 'paper.reviewEmptyHint' : 'paper.reportEmptyHint');
  var btnLabel = _tt(isReview ? 'paper.reviewGenerate' : 'paper.reportGenerate');
  container.innerHTML =
    '<div class="paper-report-empty">' +
      '<p>' + escapeHtml(title) + '</p>' +
      '<p class="paper-report-hint">' + escapeHtml(hint) + '</p>' +
      '<button class="paper-report-generate-btn" onclick="' + genFn + '">' +
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l14 9-14 9V3z"/></svg>' +
        '<span>' + escapeHtml(btnLabel) + '</span>' +
      '</button>' +
    '</div>';
}

/** Review-Mode entry: load-or-generate against the review view-context. */
async function _loadOrGenerateReview() {
  return _loadOrGenerateReport(_reportView('review'));
}


// ── Report Model Picker ──

/** Populate a view's model dropdown from _registeredModels (set by main.js).
 *  `view` defaults to the report view (historical callers pass nothing). */
function _populatePaperReportModelDropdown(view) {
  view = view || _reportView('report');
  var dropdown = document.getElementById(view.modelDropdownId);
  if (!dropdown) return;
  var models = (typeof _registeredModels !== 'undefined') ? _registeredModels : [];
  var hiddenSet = (typeof _hiddenModels !== 'undefined') ? _hiddenModels : new Set();

  dropdown.innerHTML = '';

  // Filter to chat-capable visible models
  var chatModels = models.filter(function(m) {
    if (hiddenSet.has(m.model_id)) return false;
    var caps = m.capabilities || [];
    for (var i = 0; i < caps.length; i++) {
      if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
    }
    return true;
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
    var preset = (typeof config !== 'undefined' && config && config.model)
      ? config.model
      : ((typeof serverModel !== 'undefined' && serverModel) ? serverModel : '');
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

  var pids = Object.keys(grouped);
  for (var pi = 0; pi < pids.length; pi++) {
    var group = grouped[pids[pi]];
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
  }
  // Keep the EN/中 segmented control in sync on every paint (both views).
  if (typeof _syncReportLangToggle === 'function') _syncReportLangToggle(view);
  // Review-only: a fresh run invalidates any cached translation reading view;
  // keep the translate toggle's state in sync on every paint.
  if (view.kind === 'review') {
    if (running) {
      _paperReviewShowTranslation = false;
      _paperReviewTranslatedText = '';
    }
    if (typeof _syncReviewTranslateBtn === 'function') _syncReviewTranslateBtn();
  }
}

/** Stop an in-flight generation. Signals the server-side task to abort
 *  (best-effort) and reflects the stopping state immediately; the `aborted`
 *  terminal event then arrives via the normal poll loop and freezes whatever
 *  partial output was produced. */
function _stopPaperReport(view) {
  view = view || _reportView('report');
  var s = view.stream;
  if (!s || s.status !== 'running') return;
  // Record the stop intent so a poll-driven repaint (which sees the task still
  // `running` until the server's `aborted` event lands) does NOT re-enable the
  // button and let the user click Stop a second time.
  s.stopRequested = true;
  var stopBtn = document.getElementById(view.stopBtnId);
  if (stopBtn) {
    stopBtn.disabled = true;
    var lbl = stopBtn.querySelector('span');
    if (lbl) lbl.textContent = (typeof t === 'function') ? t('paper.reportStopping') : 'Stopping…';
  }
  // Stop pressed while /start is still in flight (no task_id yet): record the
  // intent so the post-start attach can abort the task the instant its id is
  // known. Without this the Stop is silently dropped and the task runs on.
  if (!s.taskId) {
    s.pendingStop = true;
    return;
  }
  Api.paper.reportAbort(s.taskId).catch(function(e) {
    console.warn('[Paper:Report] stop request failed:', e);
  });
  // Don't flip status locally — the server emits the authoritative `aborted`
  // event, which the poll loop applies and repaints. Re-enable the label on
  // the next paint cycle so a failed abort doesn't leave the button stuck.
  //
  // Safety net: if the abort request fails OR the server never emits the
  // `aborted` event (task died, poll loop stalled), `status` would stay
  // 'running' forever — Stop disabled + Regenerate hidden = a permanently
  // frozen toolbar (the reported freeze). After a grace period, if this exact
  // stream is still 'running' and we asked it to stop, force a local terminal
  // state so Regenerate reappears and the user can recover.
  setTimeout(function() {
    if (view.stream === s && s.status === 'running' && s.stopRequested) {
      console.warn('[Paper:Report] abort not confirmed by server — forcing local aborted state for task ' + s.taskId);
      s.status = 'aborted';
      if (s.pollTimer) { clearTimeout(s.pollTimer); s.pollTimer = null; }
      if (s.paperId === _activePaperId) _paintReportFromState(view);
    }
  }, 8000);
}

function _stopPaperReview() { return _stopPaperReport(_reportView('review')); }

async function _regeneratePaperReport(view) {
  view = view || _reportView('report');
  // Atomic regenerate: the backend's force=true /start aborts the old task
  // AND registers the new task_id over the dedup index in ONE transaction
  // (routes/paper.py). So we do NOT fire a separate /abort — that split the
  // "stop + restart" into two fire-and-forget requests, and a refresh between
  // them left the old task half-aborted with no new task registered, so
  // re-entry reverted to the stale DB cache (orphan running task).
  var reportLang = view.uiLang();
  // ★ Persist the regenerate INTENT synchronously, BEFORE the await below, so
  //   a refresh that interrupts the force /start round-trip still resumes the
  //   regenerate on re-entry instead of showing the old report.
  _setReportRegenIntent(_paperHash, reportLang, view.regenIntentKey);
  _resetReportLocalState(view);
  view.cache = '';
  await _generatePaperReport(true, view);
}

async function _regeneratePaperReview() { return _regeneratePaperReport(_reportView('review')); }

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
  if (!_activePaperId || !_paperHash) {
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

/** Persist the author-rebuttal paste per paper so it survives tab switches and
 *  reloads (keyed by paper id, like the venue/lang maps). Lives in
 *  report.js next to the other paper-scoped state writers. */
function _onRebuttalInputChange(val) {
  _paperRebuttalInputText = val || '';
  try {
    if (typeof _activePaperId !== 'undefined' && _activePaperId) {
      var raw = localStorage.getItem('paper_rebuttal_text_by_id');
      var map = raw ? JSON.parse(raw) : {};
      if (_paperRebuttalInputText) map[_activePaperId] = _paperRebuttalInputText;
      else delete map[_activePaperId];
      localStorage.setItem('paper_rebuttal_text_by_id', JSON.stringify(map));
    }
  } catch (e) { console.warn('[Paper:Rebuttal] persist input failed:', e); }
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
  paperId = paperId || _activePaperId;
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
    var stored = _readVenueMap()[_activePaperId];
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
  if (key && _activePaperId && !silent) _persistReviewVenue(_activePaperId, key);
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
    _resetReportLocalState(_reportView('review'));
    _paperReviewCache = '';
    _loadOrGenerateReview();
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
