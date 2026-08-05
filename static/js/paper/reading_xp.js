/* ═══════════════════════════════════════════════════════════════════
   paper/reading_xp.js — Reading-experience rail (design
   docs/PAPER_READING_EXPERIENCE_DESIGN.md P0)

   Turns the insight second-pass from a single end-of-report block into
   ANCHORED cards distributed through the reading flow:

     • connections / provocations carrying a resolved ``anchor_idx`` land as
       cards right after that section's heading (the backend resolved the
       model's nomination against the report's real h2/h3 sequence — the
       index here enumerates the same sequence, so the two never disagree);
     • thesis / opinion / open problems / UNANCHORED items rebuild the
       classic 💡 section at the end (nothing the pass produced is lost);
     • a 📦 recap card closes the report (thesis + top connections + the
       first open problem) — the "what you're taking away" moment;
     • the finish tag shows the TOTAL cost (body + second passes) with a
       per-pass breakdown tooltip (cost visibility, design §3.3).

   Seams into report.js (all optional-chained so a stale bundle degrades to
   the legacy appended-markdown behaviour):
     • window._paperXpAfterRender(article, container, view) — end of
       _renderFinalReport (covers cached + re-rendered reports);
     • window._paperXpHandleInsightEvent(s, ev, view) — the v2 'insight'
       event (structured items) → distribute into the LIVE article;
     • window._paperXpApplyMetaEvent(s, ev, view) — the 'report_meta' event
       (secondPasses landed after `done`) → hot-update the finish tag.

   All state is `var` on window — no top-level let/const (bundle concat).
   ═══════════════════════════════════════════════════════════════════ */

/* ── xp payload store ──────────────────────────────────────────────────
 * `_reportView(kind)` returns a FRESH object literal on every call — only
 * its getter/setter-backed props (cache/meta/stream/model) share state via
 * module globals. Ad-hoc props (view._xpInsight / _xpCheckpoints /
 * _paperNotes) set on one instance are INVISIBLE to the next instance, so
 * every xp payload lives HERE, keyed by (paper, view-kind, lang): writes go
 * to the store AND the passed instance (back-compat), reads prefer the
 * store (cross-instance) and fall back to the instance.
 */
var _paperXpStore = {};

function _xpKey(view) {
  try {
    var pid = (typeof _activePaperId !== 'undefined') ? (_activePaperId || '') : '';
    var lk = (view && typeof view.langKey === 'function') ? view.langKey() : '';
    return pid + '::' + ((view && view.kind) || '') + '::' + lk;
  } catch (e) {
    return '';
  }
}

function _xpGet(view, name) {
  var k = _xpKey(view);
  var s = k && _paperXpStore[k];
  if (s && s[name] !== undefined) return s[name];
  return view ? view[name] : undefined;
}

function _xpSet(view, name, val) {
  var k = _xpKey(view);
  if (k) {
    var s = _paperXpStore[k] || (_paperXpStore[k] = {});
    s[name] = val;
  }
  if (view) view[name] = val;
}

/** Grounded ref → arXiv link HTML (mirrors lib/paper/insight_engine/_render._ref_md). */
function _xpRefLink(card) {
  if (!card || !card.arxiv_id) return '';
  var url = card.abs_url || ('https://arxiv.org/abs/' + card.arxiv_id);
  var title = card.title || card.arxiv_id;
  return ' (<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">' +
    escapeHtml(title) + '</a>)';
}

function _xpCard(cls, icon, title, innerHtml) {
  return '<div class="paper-xp-card ' + cls + '" role="note">' +
    '<div class="paper-xp-card-head">' + icon +
    '<span class="paper-xp-card-title">' + escapeHtml(title) + '</span></div>' +
    '<div class="paper-xp-card-body">' + innerHtml + '</div></div>';
}

function _xpConnectionCardHtml(item) {
  var label = (typeof t === 'function') ? t('paper.xpConnTitle') : 'Connections to your reading';
  var body = '<div class="paper-xp-conn">' + escapeHtml(item.text || '') +
    _xpRefLink(item.paper) + '</div>';
  return _xpCard('xp-conn', '🔗', label, body);
}

function _xpActionBtn(kind, text, i18nKey, fallback) {
  var label = (typeof t === 'function') ? t(i18nKey) : fallback;
  return '<button type="button" class="paper-xp-act xp-act-' + kind +
    '" data-text="' + escapeHtml(text) + '">' + escapeHtml(label) + '</button>';
}

function _xpProvocationCardHtml(item) {
  var label = (typeof t === 'function') ? t('paper.xpProvTitle') : 'Pause and think';
  var text = (typeof item === 'string') ? item : (item.text || '');
  return _xpCard('xp-prov', '💭', label,
    '<div class="paper-xp-prov">' + escapeHtml(text) + '</div>' +
    '<div class="paper-xp-actions">' +
      _xpActionBtn('debate', text, 'paper.xpDebate', 'Debate this') +
    '</div>');
}

function _xpOpenProblemCardHtml(op) {
  var label = (typeof t === 'function') ? t('paper.xpOpenTitle') : 'Worth your Monday';
  var text = (op && op.text) || '';
  return _xpCard('xp-open', '🧭', label,
    '<div class="paper-xp-open">' + escapeHtml(text) + _xpRefLink(op && op.grounded_by) + '</div>' +
    '<div class="paper-xp-actions">' +
      _xpActionBtn('ideate', text, 'paper.xpIdeate', 'Turn into a proposal') +
    '</div>');
}

/** Remove every xp node we previously inserted (idempotent re-distribution). */
function _xpClear(article) {
  if (!article || !article.querySelectorAll) return;
  var nodes = article.querySelectorAll('.paper-xp-card, .paper-xp-section, .paper-xp-recap');
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].parentNode) nodes[i].parentNode.removeChild(nodes[i]);
  }
}

/** Distribute the insight payload through the rendered article.
 *  `view._xpInsight` = {items, markdown}. Safe to call repeatedly. */
function _paperXpDistribute(article, view) {
  if (!article || !view) return;
  var payload = _xpGet(view, '_xpInsight');
  var items = payload && payload.items;
  _xpClear(article);
  if (!items || typeof items !== 'object') return;
  var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var headings = article.querySelectorAll('h2, h3');

  // ── Anchored cards: insert right after the resolved section heading. ──
  var endConns = [];
  var conns = Array.isArray(items.connections) ? items.connections : [];
  for (var i = 0; i < conns.length; i++) {
    var c = conns[i];
    if (!c || !(c.text || '').trim()) continue;
    var idx = (typeof c.anchor_idx === 'number') ? c.anchor_idx : null;
    if (idx !== null && headings[idx]) {
      headings[idx].insertAdjacentHTML('afterend', _xpConnectionCardHtml(c));
    } else {
      endConns.push(c);
    }
  }
  var endProvs = [];
  var provs = Array.isArray(items.provocations) ? items.provocations : [];
  for (var j = 0; j < provs.length; j++) {
    var p = provs[j];
    var ptext = (typeof p === 'string') ? p : (p && p.text) || '';
    if (!ptext.trim()) continue;
    var pidx = (p && typeof p === 'object' && typeof p.anchor_idx === 'number')
      ? p.anchor_idx : null;
    if (pidx !== null && headings[pidx]) {
      headings[pidx].insertAdjacentHTML('afterend', _xpProvocationCardHtml(p));
    } else {
      endProvs.push(ptext);
    }
  }

  // ── End section: everything unanchored + thesis/opinion/open problems. ──
  var H = zh
    ? { sec: '## 💡 洞见与灵感', thesis: '这篇论文的赌注', conns: '与你读过的工作的联系',
        opinion: '一个观点', open: '值得你周一动手的开放问题', prov: '挑衅式追问' }
    : { sec: '## 💡 Insight & Ideas', thesis: 'The Bet', conns: 'Connections to Your Reading',
        opinion: 'A Take', open: 'Open Problems Worth Your Monday', prov: 'Provocations' };
  var md = [];
  var thesis = (items.thesis || '').trim();
  var opinion = (items.opinion || '').trim();
  var ops = (Array.isArray(items.open_problems) ? items.open_problems : [])
    .filter(function (o) { return o && (o.text || '').trim(); });
  var hasEnd = thesis || opinion || endConns.length || ops.length || endProvs.length;
  if (hasEnd) {
    md.push(H.sec, '');
    if (thesis) {
      md.push('### ' + H.thesis, '',
        (zh ? '> 关键结论：' : '> Key takeaway: ') + thesis, '');
    }
    if (endConns.length) {
      md.push('### ' + H.conns, '');
      for (var k = 0; k < endConns.length; k++) {
        md.push('- ' + endConns[k].text.trim() +
          (endConns[k].paper && endConns[k].paper.arxiv_id
            ? ' ([' + (endConns[k].paper.title || endConns[k].paper.arxiv_id) + '](' +
              (endConns[k].paper.abs_url || ('https://arxiv.org/abs/' + endConns[k].paper.arxiv_id)) + '))'
            : ''));
      }
      md.push('');
    }
    if (opinion) md.push('### ' + H.opinion, '', opinion, '');
    var sec = document.createElement('div');
    sec.className = 'paper-xp-section';
    sec.innerHTML = (typeof renderMarkdown === 'function')
      ? renderMarkdown(md.join('\n')) : escapeHtml(md.join('\n'));
    // Open problems + unanchored provocations render as ACTION cards (debate
    // / ideate buttons), not static markdown list items — P1 启发:每个念头
    // 都有一个可以按下去的按钮.
    if (ops.length) {
      var opsTitle = document.createElement('h3');
      opsTitle.textContent = H.open;
      sec.appendChild(opsTitle);
      for (var m = 0; m < ops.length; m++) {
        sec.insertAdjacentHTML('beforeend', _xpOpenProblemCardHtml(ops[m]));
      }
    }
    if (endProvs.length) {
      var provTitle = document.createElement('h3');
      provTitle.textContent = H.prov;
      sec.appendChild(provTitle);
      for (var n = 0; n < endProvs.length; n++) {
        sec.insertAdjacentHTML('beforeend', _xpProvocationCardHtml(endProvs[n]));
      }
    }
    article.appendChild(sec);
  }

  // ── Recap card: what you're taking away. Always at the very end. ──
  if (thesis || conns.length || ops.length) {
    var recapTitle = (typeof t === 'function') ? t('paper.xpRecapTitle') : 'What you are taking away';
    var rows = [];
    if (thesis) rows.push('<div class="paper-xp-recap-row"><b>' +
      escapeHtml(zh ? '赌注' : 'The bet') + ':</b> ' + escapeHtml(thesis) + '</div>');
    var top = conns.slice(0, 2);
    for (var r = 0; r < top.length; r++) {
      rows.push('<div class="paper-xp-recap-row">🔗 ' + escapeHtml(top[r].text || '') + '</div>');
    }
    if (ops.length) {
      rows.push('<div class="paper-xp-recap-row">🧭 ' + escapeHtml(ops[0].text || '') + '</div>');
    }
    var recap = document.createElement('div');
    recap.className = 'paper-xp-recap';
    recap.setAttribute('role', 'note');
    recap.innerHTML = '<div class="paper-xp-recap-head">📦 ' + escapeHtml(recapTitle) + '</div>' +
      rows.join('');
    article.appendChild(recap);
  }
}

/* ── Checkpoint flip cards (P2 易懂:主动回忆) ─────────────────────────── */

function _xpCheckpointCardHtml(item) {
  var title = (typeof t === 'function') ? t('paper.xpCheckpointTitle') : 'Checkpoint';
  var hint = (typeof t === 'function') ? t('paper.xpFlipHint') : 'tap to reveal the answer';
  return '<div class="paper-xp-flip" role="button" tabindex="0" '
    + 'aria-label="' + escapeHtml(item.question) + '">' +
    '<div class="paper-xp-flip-face paper-xp-flip-front">' +
      '<div class="paper-xp-card-head">🧠' +
      '<span class="paper-xp-card-title">' + escapeHtml(title) + '</span>' +
      '<span class="paper-xp-flip-hint">' + escapeHtml(hint) + '</span></div>' +
      '<div class="paper-xp-flip-q">' + escapeHtml(item.question) + '</div>' +
    '</div>' +
    '<div class="paper-xp-flip-face paper-xp-flip-back">' +
      '<div class="paper-xp-card-head">✅' +
      '<span class="paper-xp-card-title">' + escapeHtml(title) + '</span></div>' +
      '<div class="paper-xp-flip-a">' + escapeHtml(item.answer) + '</div>' +
    '</div></div>';
}

/** Insert checkpoint flip cards at the END of their anchored sections
 *  (right before the next h2/h3 — the natural pause point). Idempotent. */
function _paperXpDistributeCheckpoints(article, view) {
  if (!article || !view) return;
  var payload = _xpGet(view, '_xpCheckpoints');
  var items = payload && payload.items;
  // Clear only prior flip cards (the insight cards/section are cleared by
  // their own distributor).
  var old = article.querySelectorAll('.paper-xp-flip');
  for (var i = 0; i < old.length; i++) {
    if (old[i].parentNode) old[i].parentNode.removeChild(old[i]);
  }
  if (!Array.isArray(items) || !items.length) return;
  var headings = article.querySelectorAll('h2, h3');
  for (var k = 0; k < items.length; k++) {
    var it = items[k];
    if (!it || typeof it.anchor_idx !== 'number' || !headings[it.anchor_idx]) continue;
    var h = headings[it.anchor_idx];
    // Section end = right before the next heading sibling (or the article end).
    var boundary = h.nextElementSibling;
    while (boundary && !/^H[23]$/.test(boundary.tagName || '')) {
      boundary = boundary.nextElementSibling;
    }
    var card = _xpCheckpointCardHtml(it);
    if (boundary) boundary.insertAdjacentHTML('beforebegin', card);
    else article.insertAdjacentHTML('beforeend', card);
  }
}

/** Live 'checkpoints' event: stash + distribute (replay-guarded). */

function _paperXpHandleCheckpointsEvent(s, ev, view) {
  if (!ev || ev.type !== 'checkpoints' || !Array.isArray(ev.items)) return false;
  if (s._checkpointsApplied) return true;
  s._checkpointsApplied = true;
  s._xpCheckpoints = { items: ev.items };
  _xpSet(view, '_xpCheckpoints', s._xpCheckpoints);
  try {
    var container = view && document.getElementById(view.containerId);
    var article = container && container.querySelector('.paper-report-article');
    if (article) _paperXpDistributeCheckpoints(article, view);
  } catch (e) {
    console.warn('[Paper:XP] live checkpoint distribute failed (non-fatal):', e);
  }
  return true;
}

/* ── Skim mode (P2 易懂:零 LLM 确定性折叠) ───────────────────────────────
 * Each section collapses to its FIRST paragraph + callout blockquotes + the
 * reading-xp cards (the tutor layer stays visible in skim — it is the
 * highest-signal content per pixel). Deterministic DOM surgery, no model.
 */

function _xpSkimApply(article, on) {
  if (!article) return;
  var kids = article.children;
  var seenParaInSection = false;
  for (var i = 0; i < kids.length; i++) {
    var el = kids[i];
    var tag = el.tagName || '';
    if (/^H[1-6]$/.test(tag)) {           // headings always stay (incl. the H1 title)
      seenParaInSection = false;
      el.classList.remove('xp-skim-hidden');
      continue;
    }
    // xp cards / sections / recap / finish tag / flip cards always stay.
    if (el.classList && (el.classList.contains('paper-xp-card')
        || el.classList.contains('paper-xp-section')
        || el.classList.contains('paper-xp-recap')
        || el.classList.contains('paper-xp-flip')
        || el.classList.contains('paper-report-finish-tag')
        || el.classList.contains('paper-terminology-audit')
        || el.classList.contains('paper-citation-audit'))) {
      el.classList.remove('xp-skim-hidden');
      continue;
    }
    if (!on) {
      el.classList.remove('xp-skim-hidden');
      continue;
    }
    // Skim = "structure-only": it hides LONG-FORM PROSE (2nd+ paragraph of
    // each section) and keeps everything scannable — the first paragraph,
    // callouts, TABLES (paper card / glossary / results), LISTS (design
    // chains), code, figures and math. Hiding structure was the v1 bug the
    // owner screenshot exposed: for table/list-dense reports it blanked the
    // chapter entirely.
    var keep = false;
    if (tag === 'P') {
      if (!seenParaInSection) { keep = true; seenParaInSection = true; }
    } else if (/^(BLOCKQUOTE|TABLE|UL|OL|PRE|FIGURE)$/.test(tag)) {
      keep = true;
    } else if (tag === 'DIV' && el.querySelector
               && el.querySelector('img, table, .katex-display')) {
      keep = true;   // framed figures / display-math wrappers
    }
    el.classList.toggle('xp-skim-hidden', !keep);
  }
}

/** Toolbar toggle: collapse the report to a skim read / restore full text. */
function _paperXpSkimToggle() {
  var container = document.getElementById('paperReportContent');
  if (!container) return;
  var article = container.querySelector('.paper-report-article');
  if (!article) return;
  var on = !container.classList.contains('paper-xp-skim-on');
  container.classList.toggle('paper-xp-skim-on', on);
  _xpSkimApply(article, on);
  // Sync every skim button label/state (report toolbar).
  var label = on
    ? ((typeof t === 'function') ? t('paper.xpSkimFull') : 'Full')
    : ((typeof t === 'function') ? t('paper.xpSkim') : 'Skim');
  var btns = document.querySelectorAll('.paper-skim-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].classList.toggle('is-on', on);
    var lab = btns[i].querySelector('.paper-skim-label');
    if (lab) lab.textContent = label;
    btns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

/** Re-apply skim after a re-render (the article was rebuilt from scratch). */
function _paperXpSkimReapply(container) {
  if (!container || !container.classList.contains('paper-xp-skim-on')) return;
  var article = container.querySelector('.paper-report-article');
  if (article) _xpSkimApply(article, true);
}

/** End-of-render seam: distribute when a payload is attached to the view. */
/** End-of-render seam: distribute when a payload is attached to the view. */
function _paperXpAfterRender(article, container, view) {
  try {
    if (view && _xpGet(view, '_xpInsight')) _paperXpDistribute(article, view);
    if (view && _xpGet(view, '_xpCheckpoints')) _paperXpDistributeCheckpoints(article, view);
    // On-demand depth buttons (P3) — deepen.js loads after this file and
    // registers its seam on window; absent → no buttons, no error.
    if (typeof window._paperDeepenAfterRender === 'function') {
      window._paperDeepenAfterRender(article, container, view);
    }
    // Margin notes (P4) + focus mode block-list refresh (P4).
    if (typeof window._paperNotesAfterRender === 'function') {
      window._paperNotesAfterRender(article, container, view);
    }
    if (typeof window._paperFocusAfterRender === 'function') {
      window._paperFocusAfterRender(article, container, view);
    }
    _paperXpSkimReapply(container);
  } catch (e) {
    console.warn('[Paper:XP] after-render distribute failed (non-fatal):', e);
  }
}

/** Live v2 'insight' event: stash the structured payload and distribute into
 *  the already-rendered article. Returns true when the event was handled as
 *  v2 (caller then SKIPS the legacy markdown-append path). */
function _paperXpHandleInsightEvent(s, ev, view) {
  if (!ev || !ev.items || typeof ev.items !== 'object') return false;
  s._insightRunning = false;
  if (s._insightApplied) return true;   // cursor replay guard
  s._insightApplied = true;
  s.insightText = ev.insight || '';
  s._xpInsight = { items: ev.items, markdown: ev.insight || '' };
  _xpSet(view, '_xpInsight', s._xpInsight);
  try {
    var container = view && document.getElementById(view.containerId);
    var article = container && container.querySelector('.paper-report-article');
    if (article) _paperXpDistribute(article, view);
  } catch (e) {
    console.warn('[Paper:XP] live distribute failed (non-fatal):', e);
  }
  return true;
}

/** 'report_meta' event: secondPasses landed after `done` — hot-update the
 *  finish tag in place (rebuild it from the fresh meta and swap the node). */
function _paperXpApplyMetaEvent(s, ev, view) {
  if (!ev || ev.type !== 'report_meta' || !ev.meta) return false;
  s.meta = ev.meta;
  if (view) view.meta = ev.meta;
  try {
    var container = view && document.getElementById(view.containerId);
    var old = container && container.querySelector('.paper-report-finish-tag');
    if (old && typeof _renderReportFinishTag === 'function') {
      var html = _renderReportFinishTag(ev.meta);
      if (html) {
        var wrap = document.createElement('div');
        wrap.innerHTML = html;
        if (wrap.firstChild) old.parentNode.replaceChild(wrap.firstChild, old);
      }
    }
  } catch (e) {
    console.warn('[Paper:XP] report_meta finish-tag refresh failed (non-fatal):', e);
  }
  return true;
}

/** Total-cost breakdown for the finish tag tooltip: "报告 ¥A + 洞察 ¥B + …".
 *  Returns '' when there are no passes (the tag then shows the body cost). */
function _paperXpCostBreakdown(meta) {
  if (!meta || !meta.secondPasses) return '';
  var parts = [];
  var fmt = function (v) {
    if (typeof v !== 'number' || v <= 0) return null;
    return (typeof formatCny === 'function') ? formatCny(v) : ('¥' + v.toFixed(4));
  };
  var base = fmt(meta.costCny);
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  if (base) parts.push(_tt('paper.xpCostBody') + ' ' + base);
  var names = { insight: 'paper.xpPassInsight', termfill: 'paper.xpPassTermfill',
                checkpoints: 'paper.xpPassCheckpoints', deepen: 'paper.xpPassDeepen' };
  var sp = meta.secondPasses || {};
  for (var key in sp) {
    var c = fmt(sp[key] && sp[key].costCny);
    if (c) parts.push(_tt(names[key] || key) + ' ' + c);
  }
  return parts.join(' + ');
}

/** Session wrap-up (P4 沉浸): when a substantial reading session ends,
 *  a quiet toast — how long, roughly how much, how many notes taken. The
 *  closing-the-book moment that consolidates the session. */
function _paperXpSessionSummary(stats, view) {
  if (!stats || !stats.minutes) return;
  var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var mins = Math.max(1, Math.round(stats.minutes));
  var words = Math.max(0, Math.round(stats.words || 0));
  var _notes = _xpGet(view, '_paperNotes');
  var noteCount = Array.isArray(_notes) ? _notes.length : 0;
  var msg = zh
    ? ('本次阅读约 ' + mins + ' 分钟 · 覆盖约 ' + words + ' 词' +
       (noteCount ? ' · ' + noteCount + ' 条批注' : ''))
    : ('~' + mins + ' min read · ~' + words + ' words covered' +
       (noteCount ? ' · ' + noteCount + ' notes' : ''));
  if (typeof showToast === 'function') {
    showToast(msg);
  } else if (typeof debugLog === 'function') {
    debugLog(msg, 'info');
  }
}

if (typeof window !== 'undefined') {
  window._paperXpSessionSummary = _paperXpSessionSummary;
  window._paperXpGet = _xpGet;
  window._paperXpSet = _xpSet;
  window._paperXpAfterRender = _paperXpAfterRender;
  window._paperXpHandleInsightEvent = _paperXpHandleInsightEvent;
  window._paperXpApplyMetaEvent = _paperXpApplyMetaEvent;
  window._paperXpDistribute = _paperXpDistribute;
  window._paperXpDistributeCheckpoints = _paperXpDistributeCheckpoints;
  window._paperXpHandleCheckpointsEvent = _paperXpHandleCheckpointsEvent;
  window._paperXpSkimToggle = _paperXpSkimToggle;
  window._paperXpSkimApply = _xpSkimApply;
  window._paperXpCostBreakdown = _paperXpCostBreakdown;

  // Card action delegation (P1 启发): one document-level listener routes
  // every .paper-xp-act button — debate → QA tab (prefilled), ideate → the
  // auto-research console. No inline onclick, so no LoadGuard registration.
  if (!window._paperXpClickWired) {
    window._paperXpClickWired = true;
    document.addEventListener('click', function (ev) {
      // Checkpoint flip cards: click anywhere on the card flips it (the
      // action buttons inside other cards are handled below and win).
      var flip = ev.target && ev.target.closest
        ? ev.target.closest('.paper-xp-flip') : null;
      var btn = ev.target && ev.target.closest
        ? ev.target.closest('.paper-xp-act') : null;
      if (btn) {
        var text = btn.getAttribute('data-text') || '';
        if (btn.classList.contains('xp-act-debate')) {
          if (typeof _paperAskQuestion === 'function') _paperAskQuestion(text);
        } else if (btn.classList.contains('xp-act-ideate')) {
          if (typeof _startResearchJob === 'function') _startResearchJob(text);
        }
        return;
      }
      if (flip) flip.classList.toggle('is-flipped');
    });
  }
}
