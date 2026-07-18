/* ═════════════════════════════════════════════════
   paper/arxiv.js — arXiv search + describe-to-recommend + fetch-one-paper

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #2). Cohesive cluster: the arXiv title-search list, the fuzzy
   describe→grounded-recommend stream, and _fetchArxivPaper. Owns its OWN
   streaming state (_recStream / _recPaintScheduled / _paperRecommendResults
   / _paperRecommendCorrection, all `var` on window). It READS the shared
   library hub (_paperLibrary / _activePaperId / _newPaperEntryId) and the
   core search-list state (_paperSearchResults / _lastArxivSearchQuery) only
   at RUNTIME inside function bodies — those stay in the core file. `_recStream`
   is read by core's KaTeX repaint hook at runtime; because this sibling loads
   BEFORE paper-reader.js in _DEFERRED_FILES and all state is window-scope var,
   that cross-read resolves. No load-time cross-file read (recipe step-3 gate).
   ═════════════════════════════════════════════════ */

/** Heuristic: does this input look like a direct arXiv ID / URL (vs a title query)? */
function _looksLikeArxivRef(s) {
  s = (s || '').trim();
  if (/arxiv\.org\//i.test(s)) return true;
  if (/^\d{4}\.\d{4,5}(v\d+)?$/.test(s)) return true;          // 2301.12345
  if (/^[a-z-]+\/\d{7}(v\d+)?$/i.test(s)) return true;          // hep-th/0601001
  return false;
}

/**
 * Entry point from the landing input. Routes a direct arXiv ID/URL straight
 * to download, or a free-text title to the arXiv search results list.
 */
function _submitArxivQuery() {
  var input = document.getElementById('paperArxivUrl');
  var q = input?.value?.trim();
  if (!q) { debugLog('Please enter a title to search, or an arXiv URL / ID', 'warning'); return; }
  if (_looksLikeArxivRef(q)) {
    _fetchArxivPaper(q);
  } else {
    _searchArxivPapers(q);
  }
}

/** Search arXiv by title/keywords and render candidate cards. */
async function _searchArxivPapers(query) {
  var viewer = document.getElementById('paperPdfViewer');
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  if (viewer) {
    viewer.innerHTML =
      '<div class="paper-loading paper-search-loading">' +
        '<div class="paper-loading-spinner"></div>' +
        '<div>' + escapeHtml(_tt('paper.searching')) + '</div>' +
      '</div>';
  }

  try {
    var data = await Api.paper.searchArxiv(query, 12);
    var results = (data && data.ok && Array.isArray(data.results)) ? data.results : [];
    _paperSearchResults = results;
    _renderArxivSearchResults(query, results);
  } catch (e) {
    console.error('[Paper] arXiv search failed:', e);
    if (viewer) {
      viewer.innerHTML =
        '<div class="paper-error">' + escapeHtml(_tt('paper.searchFailed')) +
        '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">' +
        escapeHtml(_tt('paper.searchBack')) + '</button></div>';
    }
  }
}

/** Escape plain text but typeset inline `$…$` LaTeX via KaTeX.
 *
 * arXiv titles and abstracts routinely contain math (e.g.
 * `$\Lambda_b^0 \to J/\psi\Xi^- K^+$`). A bare escapeHtml() would show the
 * raw TeX. This splits on `$…$` (and `\(…\)`) spans, escapes the prose, and
 * renders each math span with KaTeX. When KaTeX is not yet loaded it kicks
 * off the lazy loader and falls back to the escaped raw TeX — the
 * `katex:loaded` listener repaints the candidate list once it arrives. */
function _escWithInlineMath(text) {
  var raw = (text == null) ? '' : String(text);
  if (raw.indexOf('$') === -1 && raw.indexOf('\\(') === -1) return escapeHtml(raw);
  var hasKatex = (typeof katex !== 'undefined');
  if (!hasKatex && typeof _ensureKatex === 'function') { try { _ensureKatex(); } catch (_) {} }
  // Split into alternating text / math tokens. `$…$` (non-greedy, no blank
  // `$$`) or `\(…\)`.
  var re = /\$(?!\$)((?:\\.|[^$\\])+?)\$(?!\$)|\\\(([\s\S]*?)\\\)/g;
  var out = '';
  var last = 0;
  var m;
  while ((m = re.exec(raw)) !== null) {
    out += escapeHtml(raw.slice(last, m.index));
    var tex = (m[1] != null ? m[1] : m[2]).trim();
    if (hasKatex) {
      try {
        out += katex.renderToString(tex, { throwOnError: false, displayMode: false, strict: false, trust: true });
      } catch (e) {
        out += '<code class="math-error">' + escapeHtml(tex) + '</code>';
      }
    } else {
      out += '<code class="math-pending">' + escapeHtml(tex) + '</code>';
    }
    last = re.lastIndex;
  }
  out += escapeHtml(raw.slice(last));
  return out;
}

/** Render the list of arXiv search-result cards. */
function _renderArxivSearchResults(query, results) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  _lastArxivSearchQuery = query;
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };

  var header =
    '<div class="paper-search-head">' +
      '<button class="paper-search-back" onclick="_showPaperLanding()" title="' + escapeHtml(_tt('paper.searchBack')) + '">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
      '</button>' +
      '<div class="paper-search-head-text">' +
        '<div class="paper-search-head-title">' + escapeHtml(_tt('paper.searchResultsTitle')) + '</div>' +
        '<div class="paper-search-head-q">“' + escapeHtml(query) + '”</div>' +
      '</div>' +
    '</div>';

  if (!results.length) {
    viewer.innerHTML =
      '<div class="paper-search">' + header +
        '<div class="paper-search-empty">' + escapeHtml(_tt('paper.searchNoResults')) + '</div>' +
      '</div>';
    return;
  }

  var hint = '<div class="paper-search-hint">' + escapeHtml(_tt('paper.searchResultsHint')) + '</div>';

  var cards = results.map(function(r, i) {
    var authors = Array.isArray(r.authors) ? r.authors : [];
    var authorStr = authors.slice(0, 4).join(', ') + (authors.length > 4 ? ' et al.' : '');
    var meta = [];
    if (r.primary_category) meta.push('<span class="paper-card-cat">' + escapeHtml(r.primary_category) + '</span>');
    if (r.published) meta.push('<span class="paper-card-date">' + escapeHtml(r.published) + '</span>');
    meta.push('<span class="paper-card-id">arXiv:' + escapeHtml(r.arxiv_id) + '</span>');
    return '' +
      '<div class="paper-result-card" role="button" tabindex="0" data-idx="' + i + '"' +
           ' onclick="_openArxivResult(' + i + ')"' +
           ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openArxivResult(' + i + ')}">' +
        '<div class="paper-result-num">' + (i + 1) + '</div>' +
        '<div class="paper-result-body">' +
          '<div class="paper-result-title">' + _escWithInlineMath(r.title || r.arxiv_id) + '</div>' +
          (authorStr ? '<div class="paper-result-authors">' + escapeHtml(authorStr) + '</div>' : '') +
          (r.summary ? '<div class="paper-result-summary">' + _escWithInlineMath(r.summary) + '</div>' : '') +
          '<div class="paper-result-meta">' + meta.join('') + '</div>' +
        '</div>' +
        '<div class="paper-result-arrow">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
        '</div>' +
      '</div>';
  }).join('');

  viewer.innerHTML =
    '<div class="paper-search">' + header + hint +
      '<div class="paper-result-list">' + cards + '</div>' +
    '</div>';
}

/** Load the arXiv paper at index `idx` of the last search results. */
function _openArxivResult(idx) {
  var r = _paperSearchResults && _paperSearchResults[idx];
  if (!r || !r.arxiv_id) return;
  _fetchArxivPaper(r.arxiv_id);
}


// ── Describe-to-recommend (fuzzy description → grounded arXiv papers) ──
// Streamed the same way as the Q&A tab beside it: a server-owned task
// (recommend/start) is polled (recommend/poll) and each grounded card is
// revealed the instant it resolves. `_recStream` is the single source of
// truth the reconciling renderer paints from — never rebuilt wholesale.
var _paperRecommendResults = [];   // last recommend candidate list (index → card)
var _paperRecommendCorrection = null;  // grounded correction paper offered by the banner
var _recStream = null;             // live streaming state (see _newRecStream)
var _recPaintScheduled = false;    // rAF-coalescing latch

function _newRecStream(description) {
  return {
    description: description,
    taskId: null,
    cursor: 0,
    status: 'pending',       // pending|running|done|error
    candidateCount: 0,       // how many skeleton slots to reserve (grounding attempts)
    interpreted: false,      // Phase 1 landed
    researchCount: 0,        // how many research tool calls have started
    researchLabel: '',       // short label of the latest research query (for the status line)
    // Chat-compatible tool rounds for the interpretation agent's research
    // (web_search / fetch_url). Rendered by the SAME renderToolRoundsHTML the
    // report/review tabs use, so the describe flow shows chatInner's inline
    // tool timeline instead of a one-line counter.
    toolRounds: [],          // [{roundNum, toolName, query, toolCallId, toolArgs, status, results, _elapsed, ...}]
    results: [],             // grounded cards, in emit order
    correction: null,        // {note, paper} | null
    llmError: false,
    aborted: false,
  };
}

/** Entry point from the landing "describe it" textarea. */
function _submitPaperDescribe() {
  var input = document.getElementById('paperDescribeInput');
  var q = input && input.value ? input.value.trim() : '';
  if (!q) { debugLog('Describe the paper you are looking for', 'warning'); return; }
  _recommendPapers(q);
}

/** Interpret a fuzzy description and STREAM grounded arXiv cards in. */
async function _recommendPapers(description) {
  // Abort any in-flight stream (best-effort) before starting a new one.
  if (_recStream && _recStream.taskId && _recStream.status === 'running') {
    _recStream.aborted = true;
    try { Api.paper.recommendAbort(_recStream.taskId); } catch (_) {}
  }
  var s = _newRecStream(description);
  _recStream = s;
  _paperRecommendResults = s.results;
  _paperRecommendCorrection = null;

  // Paint the shell immediately (header + interpreting banner) so there is no
  // spinner-then-dump — the pre-grounding state IS the loading affordance.
  _paintRecommendFromState();

  try {
    var startData = await Api.paper.recommendStart(description, 6);
    if (!startData || !startData.ok || !startData.task_id) {
      throw new Error((startData && startData.error) || 'recommend start failed');
    }
    if (_recStream !== s) return;   // superseded by a newer submit
    s.taskId = startData.task_id;
    await _pollRecommendTask(s);
  } catch (e) {
    console.error('[Paper] recommend failed:', e);
    if (_recStream === s) { s.status = 'error'; _renderRecommendError(description); }
  }
}

/** Poll a streaming recommend task to completion, applying events to `s`. */
async function _pollRecommendTask(s) {
  var POLL_MS = 600;
  while (true) {
    if (_recStream !== s || s.aborted) break;
    var resp = await Api.paper.recommendPoll(s.taskId, s.cursor);
    if (_recStream !== s || s.aborted) break;
    if (!resp || !resp.ok) {
      if (resp && resp.status === 404) { s.status = 'error'; _paintRecommendFromState(); break; }
      throw new Error('HTTP ' + (resp ? resp.status : '?'));
    }
    var data = await resp.json();
    if (!data.ok) throw new Error((typeof data.error === 'string' ? data.error : 'Poll failed'));

    var events = data.events || [];
    for (var i = 0; i < events.length; i++) _applyRecommendEvent(s, events[i]);
    s.cursor = data.next_cursor;

    if (data.status === 'done') {
      s.status = 'done';
      // Authoritative final snapshot (covers any card the event replay missed).
      if (Array.isArray(data.results) && data.results.length >= s.results.length) {
        s.results = data.results; _paperRecommendResults = s.results;
      }
      if (data.correction) s.correction = data.correction;
      s.llmError = !!data.llmError;
      // Sweep the final snapshot — any grounded card the streamed 'candidate'
      // events missed still gets persisted (dedup makes re-saves a no-op).
      for (var ci = 0; ci < s.results.length; ci++) _persistRecommendedCard(s.results[ci]);
      if (s.correction && s.correction.paper) _persistRecommendedCard(s.correction.paper);
      _paintRecommendFromState();
      break;
    }
    if (data.status === 'error') {
      s.status = 'error';
      s.llmError = !!data.llmError;
      _renderRecommendError(s.description);
      break;
    }
    _paintRecommendFromState();
    await new Promise(function(r) { setTimeout(r, POLL_MS); });
  }
}

/** Apply one recommend stream event to `s`. */
function _applyRecommendEvent(s, ev) {
  switch (ev.type) {
    case 'tool_start':
      // The interpretation agent is researching (web_search / fetch_url) before
      // it proposes candidates. Accumulate a chat-compatible round entry so the
      // SAME renderToolRoundsHTML the report tab uses renders the inline tool
      // timeline (user sees genuine current-literature research, not a blind
      // memory guess). researchCount/Label are kept as a status-line fallback.
      s.researchCount = (s.researchCount || 0) + 1;
      s.researchLabel = (typeof ev.query === 'string' ? ev.query : '').slice(0, 80);
      s.toolRounds.push({
        roundNum: ev.roundNum,
        toolName: ev.toolName,
        query: ev.query || ev.toolName,
        toolCallId: ev.toolCallId || '',
        toolArgs: ev.toolArgs || '',
        status: 'searching',
        results: null,
      });
      return;
    case 'tool_done': {
      var tr = null;
      for (var ti = 0; ti < s.toolRounds.length; ti++) {
        if (s.toolRounds[ti].roundNum === ev.roundNum) { tr = s.toolRounds[ti]; break; }
      }
      if (tr) {
        tr.status = 'done';
        if (typeof ev.elapsed === 'number') tr._elapsed = ev.elapsed.toFixed(1) + 's';
        if (ev.toolContent) tr.toolContent = ev.toolContent;
        if (ev.results) tr.results = ev.results;
        if (ev.searchDiag) tr.searchDiag = ev.searchDiag;
        if (ev.engineBreakdown) tr.engineBreakdown = ev.engineBreakdown;
        if (ev.verticals) tr.verticals = ev.verticals;
      }
      return;
    }
    case 'interpret_done':
      s.interpreted = true;
      s.candidateCount = (typeof ev.candidateCount === 'number') ? ev.candidateCount : 0;
      return;
    case 'candidate': {
      var idx = (typeof ev.index === 'number') ? ev.index : s.results.length;
      s.results[idx] = ev.card;
      _paperRecommendResults = s.results;
      // Auto-persist the moment the card lands so it's never lost (grounded +
      // arxiv-bearing cards only; deduped whole-library).
      _persistRecommendedCard(ev.card);
      return;
    }
    case 'correction':
      s.correction = ev.correction || null;
      _paperRecommendCorrection = (s.correction && s.correction.paper) ? s.correction.paper : null;
      // The correction "actual winner" is exactly the paper a user comes back
      // for — save it too.
      if (_paperRecommendCorrection) _persistRecommendedCard(_paperRecommendCorrection);
      return;
    case 'error':
      s.llmError = !!ev.llmError;
      return;
    default:
      return;
  }
}

/** Shared error surface for the describe flow. */
function _renderRecommendError(description) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  viewer.innerHTML =
    '<div class="paper-error">' + escapeHtml(_tt('paper.recommendFailed')) +
    '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">' +
    escapeHtml(_tt('paper.searchBack')) + '</button></div>';
}

/** Inner HTML of a grounded recommend card (index `i`). */
function _recCardInnerHtml(r, i) {
  var authors = Array.isArray(r.authors) ? r.authors : [];
  var authorStr = authors.slice(0, 4).join(', ') + (authors.length > 4 ? ' et al.' : '');
  var meta = [];
  if (r.venue) meta.push('<span class="paper-card-venue">' + escapeHtml(r.venue) + '</span>');
  if (r.primary_category) meta.push('<span class="paper-card-cat">' + escapeHtml(r.primary_category) + '</span>');
  if (r.published) meta.push('<span class="paper-card-date">' + escapeHtml(r.published) + '</span>');
  meta.push('<span class="paper-card-id">arXiv:' + escapeHtml(r.arxiv_id) + '</span>');
  return '' +
    '<div class="paper-result-num">' + (i + 1) + '</div>' +
    '<div class="paper-result-body">' +
      '<div class="paper-result-title">' + _escWithInlineMath(r.title || r.arxiv_id) + '</div>' +
      (r.why ? '<div class="paper-result-why">' +
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
        '<span>' + escapeHtml(r.why) + '</span></div>' : '') +
      (authorStr ? '<div class="paper-result-authors">' + escapeHtml(authorStr) + '</div>' : '') +
      (r.summary ? '<div class="paper-result-summary">' + _escWithInlineMath(r.summary) + '</div>' : '') +
      '<div class="paper-result-meta">' + meta.join('') + '</div>' +
    '</div>' +
    '<div class="paper-result-arrow">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
    '</div>';
}

/** Inner HTML of a skeleton (not-yet-grounded) placeholder card. */
function _recSkeletonInnerHtml() {
  return '' +
    '<div class="paper-result-num paper-rec-sk-num"></div>' +
    '<div class="paper-result-body">' +
      '<div class="paper-rec-sk-line paper-rec-sk-title"></div>' +
      '<div class="paper-rec-sk-line paper-rec-sk-why"></div>' +
      '<div class="paper-rec-sk-line paper-rec-sk-meta"></div>' +
    '</div>';
}

/** Correction-banner HTML (or '' when absent). */
function _recCorrectionHtml(correction, _tt) {
  if (!correction || !correction.note) return '';
  var offer = '';
  if (correction.paper && correction.paper.arxiv_id) {
    var cp = correction.paper;
    offer =
      '<div class="paper-correction-offer paper-result-card" role="button" tabindex="0"' +
           ' onclick="_openRecommendCorrection()"' +
           ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openRecommendCorrection()}">' +
        '<div class="paper-result-body">' +
          '<div class="paper-correction-offer-label">' + escapeHtml(_tt('paper.correctionActual')) + '</div>' +
          '<div class="paper-result-title">' + _escWithInlineMath(cp.title || cp.arxiv_id) + '</div>' +
          '<div class="paper-result-meta"><span class="paper-card-id">arXiv:' + escapeHtml(cp.arxiv_id) + '</span></div>' +
        '</div>' +
        '<div class="paper-result-arrow">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
        '</div>' +
      '</div>';
  }
  return '' +
    '<div class="paper-correction" role="note">' +
      '<div class="paper-correction-icon">' +
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
      '</div>' +
      '<div class="paper-correction-body">' +
        '<div class="paper-correction-title">' + escapeHtml(_tt('paper.correctionTitle')) + '</div>' +
        '<div class="paper-correction-note">' + escapeHtml(correction.note) + '</div>' +
        offer +
      '</div>' +
    '</div>';
}

/**
 * Paint the recommendation view from `_recStream`, reconciling in place.
 *
 * Streaming discipline (mirrors _renderPaperQA): the shell (header/banner/hint/
 * list scaffold) is built once; thereafter each card node is reconciled and
 * only rewritten when its rendered content actually changes (compare-before-
 * swap via `_recSig`). A not-yet-grounded slot is a `data-status="searching"`
 * skeleton; when its card lands the same node flips to `data-status="grounded"`
 * and its inner HTML is swapped in — the stagger + reveal animation fires from
 * CSS keyed on the status change. rAF-coalesced so a burst of poll ticks costs
 * one paint per frame.
 */
function _paintRecommendFromState() {
  if (_recPaintScheduled) return;
  _recPaintScheduled = true;
  var raf = (typeof requestAnimationFrame === 'function')
    ? requestAnimationFrame : function(fn){ return setTimeout(fn, 16); };
  raf(function() {
    _recPaintScheduled = false;
    try { _paintRecommendNow(); }
    catch (e) { console.warn('[Paper:Recommend] paint failed:', e); }
  });
}

function _paintRecommendNow() {
  var s = _recStream;
  if (!s) return;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };

  // How many card slots to show: grounded cards + skeletons up to the
  // interpreted candidate count. Before Phase 1 lands we show none (the
  // interpreting banner is the affordance) to avoid a CLS jump on count change.
  var grounded = s.results.filter(function(x){ return !!x; }).length;
  var slots = s.interpreted
    ? Math.max(grounded, (s.status === 'done') ? grounded : s.candidateCount)
    : 0;

  // ── Build the shell once (identified by data-rec-shell) ──
  var shell = viewer.querySelector('.paper-search[data-rec-shell]');
  if (!shell) {
    var header =
      '<div class="paper-search-head">' +
        '<button class="paper-search-back" onclick="_showPaperLanding()" title="' + escapeHtml(_tt('paper.searchBack')) + '">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
        '</button>' +
        '<div class="paper-search-head-text">' +
          '<div class="paper-search-head-title">' + escapeHtml(_tt('paper.recommendTitle')) + '</div>' +
          '<div class="paper-search-head-q">“' + escapeHtml(s.description) + '”</div>' +
        '</div>' +
      '</div>';
    viewer.innerHTML =
      '<div class="paper-search" data-rec-shell="1">' + header +
        '<div class="paper-rec-status" data-rec-status aria-live="polite"></div>' +
        '<div class="paper-report-tools paper-rec-tools" data-rec-tools></div>' +
        '<div class="paper-rec-banner" data-rec-banner></div>' +
        '<div class="paper-search-hint" data-rec-hint hidden>' + escapeHtml(_tt('paper.recommendHint')) + '</div>' +
        '<div class="paper-result-list" data-rec-list aria-live="polite" aria-relevant="additions"></div>' +
      '</div>';
    shell = viewer.querySelector('.paper-search[data-rec-shell]');
  }

  var statusEl = shell.querySelector('[data-rec-status]');
  var toolsEl = shell.querySelector('[data-rec-tools]');
  var bannerEl = shell.querySelector('[data-rec-banner]');
  var hintEl = shell.querySelector('[data-rec-hint]');
  var listEl = shell.querySelector('[data-rec-list]');

  // ── Status line (researching → interpreting → grounding progress → settled) ──
  // The per-query research detail now lives in the inline tool timeline below;
  // this line is just the phase summary (mirrors chatInner's phase text sitting
  // above the tool rounds).
  var statusHtml = '';
  if (!s.interpreted && s.status !== 'error') {
    if (s.researchCount > 0) {
      var researchTxt = _tt('paper.recommendResearching').replace('{n}', s.researchCount);
      statusHtml = '<span class="paper-rec-spin"></span>' + escapeHtml(researchTxt);
    } else {
      statusHtml = '<span class="paper-rec-spin"></span>' + escapeHtml(_tt('paper.recommendInterpreting'));
    }
  } else if (s.status === 'running' && grounded < slots) {
    statusHtml = '<span class="paper-rec-spin"></span>' +
      escapeHtml(_tt('paper.recommendGrounding').replace('{n}', grounded).replace('{total}', slots));
  }
  if (statusEl._recSig !== statusHtml) {
    statusEl.innerHTML = statusHtml;
    statusEl.hidden = !statusHtml;
    statusEl._recSig = statusHtml;
  }

  // ── Research tool timeline — reuse chat's unified renderer for identical
  // look & feel (the interpretation agent's web_search / fetch_url rounds). ──
  if (toolsEl) {
    var toolCount = s.toolRounds.length;
    var searchingCount = 0;
    for (var tci = 0; tci < s.toolRounds.length; tci++) {
      if (s.toolRounds[tci].status === 'searching') searchingCount++;
    }
    var toolKey = toolCount + ':' + searchingCount;
    if (toolsEl._recToolKey !== toolKey) {
      if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
        toolsEl.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
      } else {
        toolsEl.innerHTML = '';
      }
      toolsEl.hidden = toolCount === 0;
      toolsEl._recToolKey = toolKey;
    }
  }

  // ── Correction banner ──
  var bannerHtml = _recCorrectionHtml(s.correction, _tt);
  if (bannerEl._recSig !== bannerHtml) {
    bannerEl.innerHTML = bannerHtml;
    bannerEl._recSig = bannerHtml;
  }

  // ── Empty terminal state (nothing grounded, no correction) ──
  var showEmpty = (s.status === 'done' && grounded === 0 && !bannerHtml);
  var emptyHtml = showEmpty
    ? '<div class="paper-search-empty">' + escapeHtml(_tt('paper.recommendNoResults')) + '</div>' : '';
  if (hintEl) hintEl.hidden = !(grounded > 0);

  // ── Reconcile cards in place (skeleton → grounded), keyed by slot index ──
  // Remove surplus nodes first.
  while (listEl.children.length > slots) {
    listEl.removeChild(listEl.lastElementChild);
  }
  for (var i = 0; i < slots; i++) {
    var card = s.results[i];
    var node = listEl.children[i];
    if (!node) {
      node = document.createElement('div');
      node.style.setProperty('--i', String(i));   // stagger index
      listEl.appendChild(node);
    }
    var status = card ? 'grounded' : 'searching';
    var sig = card ? ('g:' + (card.arxiv_id || i)) : 'sk';
    if (node._recSig === sig) continue;            // compare-before-swap
    if (card) {
      node.className = 'paper-result-card paper-rec-card';
      node.setAttribute('role', 'button');
      node.setAttribute('tabindex', '0');
      node.setAttribute('data-idx', String(i));
      node.setAttribute('data-status', status);
      node.onclick = (function(idx){ return function(){ _openRecommendResult(idx); }; })(i);
      node.onkeydown = (function(idx){ return function(ev){
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); _openRecommendResult(idx); }
      }; })(i);
      node.innerHTML = _recCardInnerHtml(card, i);
    } else {
      node.className = 'paper-result-card paper-rec-card paper-rec-skeleton';
      node.removeAttribute('role');
      node.removeAttribute('tabindex');
      node.setAttribute('data-status', status);
      node.setAttribute('aria-hidden', 'true');
      node.onclick = null; node.onkeydown = null;
      node.innerHTML = _recSkeletonInnerHtml();
    }
    node._recSig = sig;
  }

  // Empty-state node lives after the list (only when truly nothing to show).
  var existingEmpty = shell.querySelector('.paper-search-empty');
  if (emptyHtml && !existingEmpty) {
    listEl.insertAdjacentHTML('afterend', emptyHtml);
  } else if (!emptyHtml && existingEmpty) {
    existingEmpty.remove();
  }
}

/** Load the recommended paper at index `idx` — reuses the arXiv ingest path.
 *  The card was auto-saved as a lightweight row when it landed, so reuse that
 *  row's id to ingest in place (no duplicate). */
function _openRecommendResult(idx) {
  var r = _paperRecommendResults && _paperRecommendResults[idx];
  if (!r || !r.arxiv_id) return;
  var saved = _findLibraryEntryByArxiv(r.arxiv_id);
  _fetchArxivPaper(r.arxiv_id, saved ? saved.id : undefined);
}

/** Load the correction banner's "actual winner" paper. */
function _openRecommendCorrection() {
  var r = _paperRecommendCorrection;
  if (!r || !r.arxiv_id) return;
  var saved = _findLibraryEntryByArxiv(r.arxiv_id);
  _fetchArxivPaper(r.arxiv_id, saved ? saved.id : undefined);
}

async function _fetchArxivPaper(directUrl, reuseId) {
  var url = directUrl;
  if (url == null) {
    var input = document.getElementById('paperArxivUrl');
    url = input?.value?.trim();
  }
  url = (url || '').trim();
  if (!url) { debugLog('Please enter an arXiv URL or ID', 'warning'); return; }

  _paperLoading = true;
  _renderArxivFetchProgress({ stage: 'resolve' });

  // Mint the bookshelf id up front and send it WITH the fetch so the server
  // persists the library row itself at the 'done' stage (server-authoritative
  // ingest) — the paper survives even if this tab closes mid-stream. When
  // lazily ingesting a saved recommendation we REUSE its existing row id so
  // _createPaperEntry upgrades that row in place (no duplicate).
  var _reusingExisting = !!(reuseId && _paperLibrary.some(function(p){ return p.id === reuseId; }));
  var _arxivPaperId = reuseId || _newPaperEntryId();

  try {
    var resp = await Api.paper.fetchArxivStream(url, _arxivPaperId);
    if (!resp || !resp.ok || !resp.body) {
      var errText = '';
      try { var j = await resp.json(); errText = j.error || ''; } catch (_) {}
      throw new Error(errText || ('HTTP ' + resp.status));
    }

    /** @type {any} */
    var doneData = null;
    var streamErr = '';
    var curArxivId = '';

    await readSSEStream(resp, {
      flushTail: false,
      onLine: function (line) {
        if (!line.startsWith('data: ')) return false;
        var payload = line.slice(6).trim();
        if (!payload) return false;
        var ev;
        try { ev = JSON.parse(payload); }
        catch (pe) { console.warn('[Paper:arXiv] Bad SSE payload:', pe, payload); return false; }

        if (ev.arxiv_id) curArxivId = ev.arxiv_id;
        ev.arxiv_id = ev.arxiv_id || curArxivId;

        if (ev.stage === 'error') { streamErr = ev.error || 'Fetch failed'; return true; }
        _renderArxivFetchProgress(ev);

        if (ev.stage === 'done') { doneData = ev; }
        return false;
      },
    });

    if (streamErr) throw new Error(streamErr);
    if (!doneData) throw new Error('Fetch ended without completion');

    _paperPdfUrl = apiUrl(doneData.pdf_url);
    // Extract filename from pdf_url (e.g. "/api/paper/pdf/arxiv_2301.12345.pdf")
    var _pdfMatch = /\/api\/paper\/pdf\/([^?#]+)/.exec(doneData.pdf_url || '');
    _paperPdfFilename = _pdfMatch ? decodeURIComponent(_pdfMatch[1]) : '';
    _paperArxivId = doneData.arxiv_id || curArxivId || '';
    _paperFileName = (doneData.title || '').trim() || ('arXiv:' + _paperArxivId);
    _paperParsedText = doneData.parsed_text || '';
    _paperTotalPages = doneData.total_pages || 0;
    _paperHash = doneData.paper_hash || '';
    _paperImages = Array.isArray(doneData.images) ? doneData.images : [];

    // Create library entry now that we have everything (sets _activePaperId).
    // Reuse the id we sent to the server so the client entry and the
    // server-persisted row are the same row.
    _createPaperEntry(_paperFileName, _paperPdfUrl, _paperParsedText, _paperArxivId, _arxivPaperId);
    _paperQAHistory = [];
    _paperReportCache = '';
    _paperReviewCache = '';
    _paperReviewVenue = '';
    _babelTranslatedPages = {};
    _updatePaperTitles();
    _renderPaperLibrary();

    if (doneData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + doneData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('arXiv parsed: ' + _paperTotalPages + ' pages, ' +
               (doneData.text_length || _paperParsedText.length) + ' chars' +
               (_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
               'success');
    } else {
      debugLog('[Paper] arXiv PDF loaded but no text extracted — Q&A and Report unavailable', 'warning');
    }

    await _loadPaperPdf(_paperPdfUrl);
    // Await the first persist so an immediate exit/refresh can't race it
    // (the server already persisted at ingest; this reconciles client state).
    await _saveActivePaperState();

    debugLog('Fetched arXiv:' + _paperArxivId + (doneData.cached ? ' (cached)' : ''), 'success');
  } catch (e) {
    console.error('[Paper] arXiv fetch failed:', e);
    // A failed fetch must not leave a ghost bookshelf entry. If we minted a
    // FRESH id up front, drop it — the server only persists a row at the 'done'
    // stage, so a failed fetch never wrote one. But when we were REUSING an
    // existing saved-recommendation row, leave it: it's a real persisted
    // recommendation the user can retry, not a ghost this fetch created.
    if (!_reusingExisting) {
      _paperLibrary = _paperLibrary.filter(function(p) { return p.id !== _arxivPaperId; });
      if (_activePaperId === _arxivPaperId) _setActivePaperId('');
    }
    _renderPaperLibrary();
    var viewer = document.getElementById('paperPdfViewer');
    if (viewer) viewer.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message || String(e)) + '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">Try Again</button></div>';
  } finally {
    _paperLoading = false;
  }
}
