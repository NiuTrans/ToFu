/* ═════════════════════════════════════════════════
   paper/qa.js — Paper Reading-Mode Q&A tab (ask-about-this-paper)

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #3). Q&A render + send + poll + text-selection functions. The Q&A
   STATE (_paperQAHistory / _paperQAStreaming / _paperQAAbort /
   _paperQAAbortRequested) is SHARED across clusters so it STAYS in the core
   State block; only the functions move. _ensurePaperText STAYS in core too
   (a shared PDF-text recovery helper the report path calls). All refs are
   window-scope var + runtime; loads BEFORE paper-reader.js in _DEFERRED_FILES.
   ═════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ Tab 1: Q&A
// ══════════════════════════════════════════════════════

/** Build the inner HTML for one Q&A message bubble. */
function _qaMsgInnerHtml(msg) {
  var isUser = msg.role === 'user';
  var inner = '';
  // Tool-activity panel (web_search / fetch_url) — reuse chat's renderer so
  // the look matches the report tab + chat bubbles.
  if (!isUser && Array.isArray(msg.toolRounds) && msg.toolRounds.length &&
      typeof renderToolRoundsHTML === 'function') {
    inner += '<div class="paper-qa-tools">' +
      renderToolRoundsHTML(msg.toolRounds, msg.status === 'running') + '</div>';
  }
  if (isUser) {
    inner += '<div class="paper-qa-msg-content">' + escapeHtml(msg.content) + '</div>';
  } else if (msg.content) {
    inner += '<div class="paper-qa-msg-content">' +
      (typeof renderMarkdown === 'function' ? renderMarkdown(msg.content) : escapeHtml(msg.content)) +
      '</div>';
  } else if (msg.status === 'running') {
    // Thinking / searching, no prose yet — show a small pulse.
    inner += '<div class="paper-qa-msg-content paper-qa-thinking">' +
      '<span class="thinking-dot"></span></div>';
  }
  return inner;
}

// Reconcile the Q&A message list in place. Streaming polls call this every
// ~700ms; rebuilding the whole innerHTML each time tore down and recreated
// every bubble (flicker + scroll jump + markdown re-parse). Instead we keep
// one DOM node per message and only rewrite a node whose rendered content
// actually changed — during streaming that's just the last assistant bubble.
function _renderPaperQA() {
  var container = document.getElementById('paperQAMessages');
  if (!container) return;
  if (!_paperQAHistory || _paperQAHistory.length === 0) {
    var _ttq = (typeof t === 'function') ? t : function(k){ return k; };
    container.innerHTML =
      '<div class="paper-qa-empty"><div class="paper-qa-empty-icon">' + Icon('messageCircle', 32) + '</div>' +
      '<p>' + escapeHtml(_ttq('paper.qaEmptyTitle')) + '</p>' +
      '<p class="paper-qa-hint">' + escapeHtml(_ttq('paper.qaEmptyHint')) + '</p></div>';
    return;
  }
  // Drop the empty-state placeholder (or any stale non-message node) before reconciling.
  var first = container.firstElementChild;
  if (first && !first.classList.contains('paper-qa-msg')) container.innerHTML = '';

  var nearBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < 80;
  var changed = false;

  // Remove surplus nodes (e.g. history was trimmed or a paper switch left extras).
  while (container.children.length > _paperQAHistory.length) {
    container.removeChild(container.lastElementChild);
    changed = true;
  }

  for (var j = 0; j < _paperQAHistory.length; j++) {
    var msg = _paperQAHistory[j];
    var cls = 'paper-qa-msg ' + (msg.role === 'user' ? 'paper-qa-user' : 'paper-qa-assistant');
    var inner = _qaMsgInnerHtml(msg);
    var node = container.children[j];
    if (!node) {
      node = document.createElement('div');
      container.appendChild(node);
    }
    if (node._qaCls !== cls) { node.className = cls; node._qaCls = cls; }
    if (node._qaSig !== inner) { node.innerHTML = inner; node._qaSig = inner; changed = true; }
  }

  if (changed && nearBottom) container.scrollTop = container.scrollHeight;
}


async function _sendPaperQuestion() {
  var input = document.getElementById('paperQAInput');
  var question = input?.value?.trim();
  if (!question || _paperQAStreaming) return;

  if (!_paperParsedText) {
    var ok = await _ensurePaperText();
    if (!ok) {
      debugLog('No paper text available — PDF may be scanned or parsing failed', 'warning');
      return;
    }
  }

  // Recent dialogue (exclude the question we're about to add) for context.
  var historyForServer = _paperQAHistory.slice(-10).map(function(m) {
    return { role: m.role, content: m.content };
  });

  _paperQAHistory.push({ role: 'user', content: question, timestamp: Date.now() });
  // Assistant placeholder carries live tool-round state for this answer.
  var asst = { role: 'assistant', content: '', timestamp: Date.now(),
               toolRounds: [], status: 'running' };
  _paperQAHistory.push(asst);
  input.value = '';
  _paperQAStreaming = true;
  _renderPaperQA();

  var startPaperId = _activePaperId;
  try {
    var startData = await Api.paper.qaStart({
      question: question,
      paper_text: _paperParsedText,
      paper_hash: _paperHash || '',
      lang: (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en',
      history: historyForServer,
      model: (typeof _paperReportModel !== 'undefined') ? _paperReportModel : undefined,
      title: _paperFileName || '',
    });
    if (!startData || !startData.ok || !startData.task_id) {
      throw new Error((startData && startData.error) || 'Q&A start failed');
    }
    await _pollQATask(startData.task_id, asst, startPaperId);
  } catch (e) {
    asst.status = 'error';
    asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
      ((typeof t === 'function') ? t('paper.qaError') : 'Error') + ': ' + (e.message || e);
    _renderPaperQA();
    console.warn('[Paper:QA] failed:', e);
  } finally {
    _paperQAStreaming = false; _paperQAAbort = null; _saveActivePaperState();
  }
}

/** Poll a Q&A task to completion, applying events to the assistant message.
 *  Mirrors _pollReportTask but writes into the QA history entry `asst`. */
async function _pollQATask(taskId, asst, startPaperId) {
  var cursor = 0;
  var POLL_MS = 700;

  /* ★ Push transport (pt_f6aec3ad). `qa_runtime` declares push_channel='paper'
   *   and `qa_engine` appends `tool_done` the instant each tool returns — so
   *   the backend has ALWAYS been broadcasting per-tool completion in real
   *   time. This view only polled, so a search that finished at t=0 kept its
   *   spinner for up to POLL_MS. Subscribing here is the missing leg; the poll
   *   below stays as the FLOOR (a client whose WebSocket is blocked by a
   *   corporate proxy has no push channel at all).
   *
   *   Both transports carry the SAME events, so every apply on both paths
   *   routes through the shared seq gate — otherwise each delta lands twice
   *   and the answer renders doubled.
   *
   *   Keyed on `asst`: Q&A mints a NEW task per question, and `asst` is that
   *   question's own message object, so each question gets its own
   *   subscription and its own high-water mark. `isCurrent` reuses the
   *   existing abandon guard (the paper must still be the active one). */
  paperAttachPush(asst, taskId, {
    isCurrent: function () { return startPaperId === _activePaperId; },
    onEvent: function (ev) {
      var dirty = paperIngestEvent(asst, ev, _applyQAEvent);
      if (ev.type === 'done') {
        asst.status = 'done';
        if (ev.answer) asst.content = ev.answer;
        dirty = true;
      } else if (ev.type === 'error') {
        asst.status = 'error';
        dirty = true;
      }
      if (dirty) _renderPaperQA();
    },
  });

  try {
  while (true) {
    if (_paperQAAbortRequested) { _paperQAAbortRequested = false; break; }
    var resp = await Api.paper.qaPoll(taskId, cursor);
    if (!resp || !resp.ok) {
      if (resp && resp.status === 404) {
        asst.status = 'error';
        asst.content = asst.content ||
          ((typeof t === 'function') ? t('paper.qaExpired') : 'Q&A task expired.');
        break;
      }
      throw new Error('HTTP ' + (resp ? resp.status : '?'));
    }
    var data = await resp.json();
    if (!data.ok) throw new Error((typeof data.error === 'string' ? data.error : 'Poll failed'));

    var events = data.events || [];
    for (var i = 0; i < events.length; i++) {
      paperIngestEvent(asst, events[i], _applyQAEvent);
    }
    cursor = data.next_cursor;

    if (data.status === 'done') {
      asst.status = 'done';
      if (data.answer) asst.content = data.answer;
      if (startPaperId === _activePaperId) _renderPaperQA();
      break;
    }
    if (data.status === 'error') {
      asst.status = 'error';
      asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
        ((typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(data.error) : (data.error || 'Error'));
      if (startPaperId === _activePaperId) _renderPaperQA();
      break;
    }
    if (startPaperId === _activePaperId) _renderPaperQA();
    await new Promise(function(r) { setTimeout(r, POLL_MS); });
  }
  } finally {
    // Release the subscription on EVERY exit path — including abort and the
    // 404/expired branch, which the terminal-frame auto-release never sees.
    paperDetachPush(asst);
  }
}

/** Apply one Q&A event to the assistant message state (chat-compatible). */
function _applyQAEvent(asst, ev) {
  switch (ev.type) {
    case 'tool_start':
      asst.toolRounds.push({
        roundNum: ev.roundNum, toolName: ev.toolName, query: ev.query,
        toolCallId: ev.toolCallId, toolArgs: ev.toolArgs,
        status: 'searching', results: null,
      });
      return true;
    case 'tool_done': {
      for (var j = 0; j < asst.toolRounds.length; j++) {
        var r = asst.toolRounds[j];
        if (r.roundNum === ev.roundNum) {
          r.status = 'done';
          r._elapsed = (ev.elapsed != null) ? (ev.elapsed + 's') : r._elapsed;
          r.toolContent = ev.toolContent || r.toolContent;
          if (ev.results) r.results = ev.results;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
          if (ev.vertical) r.vertical = ev.vertical;
          if (ev.verticals) r.verticals = ev.verticals;
          break;
        }
      }
      return true;
    }
    case 'delta':
      asst.content += (ev.delta || '');
      return true;
    case 'delta_reset':
      // Interim draft emitted alongside a tool call — discard it (the model
      // rewrites the full answer after the tool result lands).
      asst.content = '';
      return true;
    default:
      return false;
  }
}

/** Public entry: ask the QA tab a fully-formed question (no selection
 *  involved) — used by the reading-experience rail's "debate this" buttons.
 *  Switches to the QA tab, seeds the input, sends. */
function _paperAskQuestion(text) {
  text = (text || '').trim();
  if (!text) return;
  if (typeof _paperActiveTab !== 'undefined' && _paperActiveTab !== 'qa'
      && typeof _switchPaperTab === 'function') {
    _switchPaperTab('qa');
  }
  if (typeof _setPaperMobileView === 'function') _setPaperMobileView('reader');
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  input.value = text;
  input.focus();
  setTimeout(function() { _sendPaperQuestion(); }, 100);
}

function _quotePaperSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  if (!text) return;
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
  _setPaperMobileView('reader');
  input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\n' + input.value;
  input.focus();
  sel.removeAllRanges();
  _hidePaperQuoteBar();
}

/** Ask about selected text — quote it and auto-send a question */
function _askAboutPaperSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  if (!text) return;
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
  _setPaperMobileView('reader');
  input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\nExplain this part of the paper.';
  sel.removeAllRanges();
  _hidePaperQuoteBar();
  // Auto-send after a brief delay for tab switch to settle
  setTimeout(function() { _sendPaperQuestion(); }, 100);
}

function _hidePaperQuoteBar() {
  var q = document.getElementById('paperQuoteBtn');
  if (q) q.style.display = 'none';
  var qr = document.getElementById('paperReportQuoteBtn');
  if (qr) qr.style.display = 'none';
}

function _handlePaperTextSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  var q = document.getElementById('paperQuoteBtn');
  var qr = document.getElementById('paperReportQuoteBtn');
  if (qr) qr.style.display = 'none';
  if (q) q.style.display = 'none';
  if (!text || text.length < 3) return;

  // Source A: selection inside the PDF sidebar → anchor the toolbar in
  // .paper-left (existing behaviour).
  var viewer = document.getElementById('paperPdfViewer');
  if (q && viewer && viewer.contains(sel.anchorNode)) {
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    var leftEl = document.querySelector('.paper-left');
    if (!leftEl) return;
    var lr = leftEl.getBoundingClientRect();
    q.style.display = 'flex';
    q.style.top = (rect.top - lr.top - 40) + 'px';
    q.style.left = Math.max(4, rect.left - lr.left + rect.width / 2 - 80) + 'px';
    return;
  }

  // Source B: selection inside the generated REPORT → anchor a sibling
  // toolbar in .paper-right so a confusing report passage becomes a
  // one-click question (the central UX ask).
  var reportEl = document.getElementById('paperReportContent');
  if (qr && reportEl && reportEl.contains(sel.anchorNode)) {
    var rrange = sel.getRangeAt(0);
    var rrect = rrange.getBoundingClientRect();
    var rightEl = document.querySelector('.paper-right');
    if (!rightEl) return;
    var rr = rightEl.getBoundingClientRect();
    qr.style.display = 'flex';
    qr.style.top = Math.max(4, rrect.top - rr.top - 40) + 'px';
    qr.style.left = Math.max(4, rrect.left - rr.left + rrect.width / 2 - 80) + 'px';
  }
}
