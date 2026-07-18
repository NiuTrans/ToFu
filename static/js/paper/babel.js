/* ═════════════════════════════════════════════════
   paper/babel.js — Babel PDF full-translation tab (Reading Mode Tab 3).

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #7). Leaf tab: _initBabelPdfTab / _switchBabelLang / _startBabelTranslation
   / _babelTranslateAllPages / _renderBabelResult. Owns _babelTargetLang +
   _babelTranslating (cluster-local). _babelTranslatedPages is SHARED (read by
   library-persist + enterPaperMode) so it moves here and resolves via load-
   before ordering (window-scope var, runtime reads). One runtime caller
   (_initBabelPdfTab from _switchPaperTab in core). Loads BEFORE paper-reader.js.
   ═════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ Tab 3: Babel PDF (Translation)
// ══════════════════════════════════════════════════════

var _babelTargetLang = '';
var _babelTranslatedPages = {};
var _babelTranslating = false;

function _initBabelPdfTab() {
  var container = document.getElementById('paperTranslateContent');
  if (!container) return;
  var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
  container.innerHTML =
    '<div class="babel-pdf-module">' +
      '<div class="babel-pdf-brand">' +
        '<svg class="babel-pdf-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/>' +
          '<path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/>' +
        '</svg>' +
        '<div class="babel-pdf-brand-text"><span class="babel-pdf-title">Babel PDF</span><span class="babel-pdf-subtitle">' + escapeHtml(_ttb('paper.babelSubtitle')) + '</span></div>' +
      '</div>' +
      '<div class="babel-pdf-lang-bar">' +
        '<button class="babel-pdf-lang' + (!_babelTargetLang ? ' active' : '') + '" data-lang="" onclick="_switchBabelLang(\'\', this)">' + escapeHtml(_ttb('paper.babelOriginal')) + '</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'zh' ? ' active' : '') + '" data-lang="zh" onclick="_switchBabelLang(\'zh\', this)">中文</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'en' ? ' active' : '') + '" data-lang="en" onclick="_switchBabelLang(\'en\', this)">English</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'ja' ? ' active' : '') + '" data-lang="ja" onclick="_switchBabelLang(\'ja\', this)">日本語</button>' +
      '</div>' +
      '<div class="babel-pdf-body" id="babelPdfBody"></div>' +
      '<div class="babel-pdf-status" id="babelPdfStatus"></div>' +
    '</div>';

  // Render cached result or empty state
  if (_babelTargetLang && _babelTranslatedPages[_babelTargetLang]) {
    _renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
  } else if (_babelTargetLang && _paperParsedText) {
    _startBabelTranslation();
  } else {
    var body = document.getElementById('babelPdfBody');
    if (body) {
      body.innerHTML =
        '<div class="babel-pdf-empty">' +
          '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>' +
          '<p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p>' +
          '<p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p>' +
        '</div>';
    }
  }
}

function _switchBabelLang(lang, btn) {
  document.querySelectorAll('.babel-pdf-lang').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  _babelTargetLang = lang;
  _startBabelTranslation();
}

function _startBabelTranslation() {
  var body = document.getElementById('babelPdfBody');
  var status = document.getElementById('babelPdfStatus');
  if (!body) return;

  var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
  var langNames = { zh: '中文', en: 'English', ja: '日本語' };
  if (!_babelTargetLang) {
    body.innerHTML = '<div class="babel-pdf-empty"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg><p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p><p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p></div>';
    if (status) status.textContent = '';
    return;
  }

  if (!_paperParsedText) {
    body.innerHTML = '<div class="babel-pdf-empty"><p>' + escapeHtml(_ttb('paper.babelNoPaper')) + '</p></div>';
    return;
  }

  // Check cache
  if (_babelTranslatedPages[_babelTargetLang]) {
    _renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
    if (status) status.textContent = _ttb('paper.babelCompleteCached');
    return;
  }

  var _langLabel = langNames[_babelTargetLang] || _babelTargetLang;
  var _translatingMsg = _ttb('paper.babelTranslatingTo', { lang: _langLabel });
  if (status) status.textContent = _translatingMsg;

  body.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' + escapeHtml(_translatingMsg) + '</div><div class="babel-pdf-progress"><div class="babel-pdf-progress-bar" id="babelProgressBar" style="width:0%"></div></div></div>';

  _babelTranslateAllPages(_babelTargetLang);
}

/** Server-owned translation: chunking, retry, persistence, dedup all live
 *  on the backend. The frontend just kicks off the task and polls events. */
async function _babelTranslateAllPages(lang) {
  if (_babelTranslating) return;
  _babelTranslating = true;

  var bar = document.getElementById('babelProgressBar');
  var statusEl = document.getElementById('babelPdfStatus');

  function _setProgress(done, total) {
    if (bar && total > 0) bar.style.width = Math.round((done / total) * 100) + '%';
    if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelTranslatedCount', { done: done, total: total }) : ('Translated ' + done + '/' + total + ' sections');
  }

  try {
    // (1) Try server-side cache first — instant if the same translation was
    //     done before (even on a different machine).
    if (_paperHash) {
      try {
        var cacheData = await Api.paper.translateCache(_paperHash, lang);
        if (cacheData && cacheData.ok && cacheData.text) {
          if (_babelTargetLang === lang) {
            _babelTranslatedPages[lang] = cacheData.text;
            _renderBabelResult(cacheData.text);
            _saveActivePaperState();
            if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
          }
          return;
        }
      } catch (e) {
        console.warn('[Babel] Cache lookup failed:', e);
      }
    }

    // (2) Start (or join) the server task.
    var startData = await Api.paper.translateStart({
      paper_text: _paperParsedText,
      lang: lang,
      paper_hash: _paperHash || '',
    });
    if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'Translate start failed');

    if (startData.cached && startData.text) {
      if (_babelTargetLang === lang) {
        _babelTranslatedPages[lang] = startData.text;
        _renderBabelResult(startData.text);
        _saveActivePaperState();
        if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
      }
      return;
    }

    if (startData.paper_hash) _paperHash = startData.paper_hash;
    var taskId = startData.task_id;
    if (!taskId) throw new Error('Translate task did not return task_id');

    // (3) Poll until the task completes (or the user switches language).
    var cursor = 0;
    var aggregated = [];
    while (true) {
      if (_babelTargetLang !== lang) {
        // User switched away — abort the server task to free resources.
        try {
          await Api.paper.translateAbort(taskId);
        } catch (_) {}
        return;
      }
      var pollResp = await Api.paper.translatePoll(taskId, cursor);
      if (!pollResp || !pollResp.ok) throw new Error('Poll HTTP ' + (pollResp ? pollResp.status : 'no response'));
      var pollData = await pollResp.json();
      if (!pollData.ok) throw new Error(pollData.error || 'Poll failed');
      cursor = pollData.next_cursor || cursor;

      var events = pollData.events || [];
      for (var ei = 0; ei < events.length; ei++) {
        var ev = events[ei];
        if (ev.type === 'chunk') {
          aggregated.push(ev.text || '');
          _setProgress(ev.index + 1, ev.total);
          if (_babelTargetLang === lang) {
            _renderBabelResult(aggregated.join('\n\n'));
          }
        } else if (ev.type === 'done') {
          if (_babelTargetLang === lang) {
            _babelTranslatedPages[lang] = ev.text || aggregated.join('\n\n');
            _renderBabelResult(_babelTranslatedPages[lang]);
            _saveActivePaperState();
            if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelComplete') : 'Translation complete';
          }
          return;
        } else if (ev.type === 'error') {
          var _evMsg = (typeof errorEnvelopeMessage === 'function')
            ? errorEnvelopeMessage(ev.error)
            : (typeof ev.error === 'string' ? ev.error : '');
          throw new Error(_evMsg || 'Translation failed');
        }
      }

      if (pollData.status === 'done') return;
      if (pollData.status === 'error') {
        var _pdMsg = (typeof errorEnvelopeMessage === 'function')
          ? errorEnvelopeMessage(pollData.error)
          : (typeof pollData.error === 'string' ? pollData.error : '');
        throw new Error(_pdMsg || 'Translation failed');
      }

      await new Promise(function(r) { setTimeout(r, 700); });
    }
  } catch (e) {
    console.warn('[Babel] Translation failed:', e);
    var body = document.getElementById('babelPdfBody');
    var _ttf = (typeof t === 'function') ? t : function(k){ return k; };
    if (body && _babelTargetLang === lang) {
      body.innerHTML = '<div class="paper-error">' + escapeHtml(_ttf('paper.babelFailed')) + ': ' +
                       escapeHtml(e.message || String(e)) +
                       '<br><button class="paper-retry-btn" onclick="_startBabelTranslation()">' + escapeHtml(_ttf('paper.retry')) + '</button></div>';
    }
    if (statusEl) statusEl.textContent = _ttf('paper.babelFailed');
  } finally {
    _babelTranslating = false;
  }
}

function _renderBabelResult(text) {
  var body = document.getElementById('babelPdfBody');
  if (!body) return;
  body.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(text) : '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.7">' + escapeHtml(text) + '</pre>';
}
