/* ═══════════════════════════════════════════════════════════════════
   paper/notes.js — Reader margin notes (design P4,
   docs/PAPER_READING_EXPERIENCE_DESIGN.md)

   Select text in the report → "记一笔" → the note persists server-side
   (paper_notes table) and the passage gains a highlight + a 📝 chip. The
   anchor is {heading_idx, quote}: the quote re-anchors a note after a
   report regeneration (fuzzy, first occurrence wins); a note that matches
   nothing degrades to the orphan tray at the report's end — it is NEVER
   silently dropped.

   Also: each note offers "就这条批注问 AI" (straight into the QA tab) and
   edit/delete in place. Own handwriting is the strongest immersion hook.
   ═══════════════════════════════════════════════════════════════════ */

var _paperNoteEditor = null;   // the open popover {el, noteId, anchor}
var _paperNotesFallbackView = null;

/** Resolve the report view. In production paper-reader.js provides
 *  `_reportView`; without it (stale bundle / harness), fall back to ONE
 *  memoized stand-in so the xp-store key stays stable across calls. */
function _paperNotesView() {
  if (typeof _reportView === 'function') return _reportView('report');
  if (!_paperNotesFallbackView) {
    _paperNotesFallbackView = {
      kind: 'report', containerId: 'paperReportContent', meta: null,
      langKey: function () { return 'en'; },
    };
  }
  return _paperNotesFallbackView;
}

/** Load notes for the current paper+lang onto the view (once per lang). */
async function _paperNotesLoad(view) {
  if (!view) return;
  var langKey = (typeof view.langKey === 'function') ? view.langKey() : '';
  // _paperHash is declared by paper-reader.js core state — guard the bare
  // reference: the after-render seam fires on EVERY render, including early
  // paints / stale-bundle states where it may not be set yet.
  var phash = (typeof _paperHash !== 'undefined') ? _paperHash : '';
  if (!phash || !langKey) return;
  var _g = (typeof window._paperXpGet === 'function') ? window._paperXpGet : null;
  var _s = (typeof window._paperXpSet === 'function') ? window._paperXpSet : null;
  if (_g && _g(view, '_paperNotesLang') === langKey && Array.isArray(_g(view, '_paperNotes'))) return;
  try {
    var data = await Api.paper.notesList(phash, langKey);
    if (data && data.ok && Array.isArray(data.notes)) {
      if (_s) { _s(view, '_paperNotes', data.notes); _s(view, '_paperNotesLang', langKey); }
      else { view._paperNotes = data.notes; view._paperNotesLang = langKey; }
    }
  } catch (e) {
    console.debug('[Paper:Notes] load failed (non-fatal):', e);
  }
}

function _paperNoteReportHeadings(article) {
  return (typeof window._deepenReportHeadings === 'function')
    ? window._deepenReportHeadings(article)
    : article.querySelectorAll('h2, h3');
}

/** Find the first text node in `article` containing `quote` (normalized). */
function _paperNoteFindQuote(article, quote) {
  if (!quote) return null;
  var needle = quote.replace(/\s+/g, ' ').trim().slice(0, 80);
  if (!needle) return null;
  var walker = document.createTreeWalker(article, 4 /* TEXT_NODE */, {
    acceptNode: function (n) {
      if (!n.nodeValue || !/\S/.test(n.nodeValue)) return 2;
      if (n.parentNode && /^(SCRIPT|STYLE|TEXTAREA|BUTTON)$/.test(n.parentNode.tagName)) return 2;
      return 1;
    },
  });
  var node;
  while ((node = walker.nextNode())) {
    var norm = node.nodeValue.replace(/\s+/g, ' ');
    var idx = norm.indexOf(needle);
    if (idx >= 0) return { node: node, index: idx, length: needle.length };
  }
  return null;
}

function _paperNoteChip(note) {
  return '<button type="button" class="paper-note-chip" data-note-id="' +
    escapeHtml(note.id) + '" title="' + escapeHtml(note.note) + '">📝</button>';
}

/** Decorate the rendered article with highlights / chips / orphan tray. */
function _paperNotesDecorate(article, view) {
  if (!article || !view) return;
  // Clear prior decoration (idempotent).
  var old = article.querySelectorAll('.paper-note-mark, .paper-note-chip, .paper-note-tray');
  for (var i = 0; i < old.length; i++) {
    var el = old[i];
    if (el.classList.contains('paper-note-mark')) {
      // Unwrap the highlight, keep the text.
      var parent = el.parentNode;
      while (el.firstChild) parent.insertBefore(el.firstChild, el);
      parent.removeChild(el);
    } else if (el.parentNode) {
      el.parentNode.removeChild(el);
    }
  }
  var notes = ((typeof window._paperXpGet === 'function')
    ? window._paperXpGet(view, '_paperNotes') : view._paperNotes) || [];
  if (!notes.length) return;
  var heads = _paperNoteReportHeadings(article);
  var orphans = [];
  for (var k = 0; k < notes.length; k++) {
    var note = notes[k];
    var anchor = note.anchor || {};
    var hit = _paperNoteFindQuote(article, anchor.quote || '');
    if (hit) {
      // Wrap the quoted span with a highlight carrying the note id.
      var norm = hit.node.nodeValue.replace(/\s+/g, ' ');
      var start = hit.node.nodeValue.indexOf(hit.node.nodeValue.trim().slice(0, 20));
      // Simple whole-node wrap when the quote dominates the node; otherwise
      // split at the measured index.
      try {
        var range = document.createRange();
        range.setStart(hit.node, hit.index);
        range.setEnd(hit.node, Math.min(hit.node.nodeValue.length, hit.index + hit.length));
        var mark = document.createElement('span');
        mark.className = 'paper-note-mark';
        mark.setAttribute('data-note-id', note.id);
        mark.setAttribute('title', note.note);
        range.surroundContents(mark);
      } catch (e) {
        // surroundContents can fail on partial-non-text boundaries — fall
        // back to a heading chip below.
        hit = null;
      }
    }
    if (!hit) {
      var idx = (typeof anchor.heading_idx === 'number') ? anchor.heading_idx : null;
      if (idx !== null && heads[idx]) {
        heads[idx].insertAdjacentHTML('beforeend', _paperNoteChip(note));
      } else {
        orphans.push(note);
      }
    }
  }
  if (orphans.length) {
    var tray = document.createElement('div');
    tray.className = 'paper-note-tray';
    var _tt = (typeof t === 'function') ? t : function (k) { return k; };
    tray.innerHTML = '<div class="paper-note-tray-head">📝 ' +
      escapeHtml(_tt('paper.noteOrphans')) + '</div>';
    for (var o = 0; o < orphans.length; o++) {
      var row = document.createElement('div');
      row.className = 'paper-note-tray-row';
      row.setAttribute('data-note-id', orphans[o].id);
      row.textContent = orphans[o].note;
      tray.appendChild(row);
    }
    article.appendChild(tray);
  }
}

/** Compute the anchor for the current report selection. */
function _paperNoteAnchorFromSelection() {
  var sel = window.getSelection();
  var quote = sel ? sel.toString().trim() : '';
  if (!quote) return null;
  var container = document.getElementById('paperReportContent');
  var article = container && container.querySelector('.paper-report-article');
  if (!article) return null;
  // Nearest preceding report heading = heading_idx (the shared enumeration).
  var heads = _paperNoteReportHeadings(article);
  var idx = null;
  var anchorNode = sel.anchorNode;
  var el = anchorNode && (anchorNode.nodeType === 1 ? anchorNode : anchorNode.parentNode);
  if (el) {
    for (var i = 0; i < heads.length; i++) {
      var rel = heads[i].compareDocumentPosition(el);
      if ((rel & 4) || heads[i].contains(el) || heads[i] === el) idx = i;
      else break;
    }
  }
  return { heading_idx: idx, char_offset: null, quote: quote.slice(0, 400) };
}

/** Open the note editor popover (create for a selection, or edit existing). */
function _paperNoteOpenEditor(anchor, existing, x, y) {
  _paperNoteCloseEditor();
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  var el = document.createElement('div');
  el.className = 'paper-note-editor';
  el.innerHTML =
    (anchor && anchor.quote
      ? '<div class="paper-note-editor-quote">' + escapeHtml(anchor.quote.slice(0, 120)) + '</div>'
      : '') +
    '<textarea class="paper-note-editor-input" rows="3" placeholder="' +
      escapeHtml(_tt('paper.notePlaceholder')) + '">' +
      (existing ? escapeHtml(existing.note) : '') + '</textarea>' +
    '<div class="paper-note-editor-actions">' +
      '<button type="button" class="paper-note-save">' + escapeHtml(_tt('paper.noteSave')) + '</button>' +
      (existing
        ? '<button type="button" class="paper-note-ask">' + escapeHtml(_tt('paper.noteAsk')) + '</button>' +
          '<button type="button" class="paper-note-del">' + escapeHtml(_tt('paper.noteDelete')) + '</button>'
        : '') +
      '<button type="button" class="paper-note-cancel">' + escapeHtml(_tt('paper.noteCancel')) + '</button>' +
    '</div>';
  document.body.appendChild(el);
  var vw = window.innerWidth;
  el.style.left = Math.max(8, Math.min(x - 120, vw - 300)) + 'px';
  el.style.top = Math.max(8, y + 12) + 'px';
  _paperNoteEditor = { el: el, noteId: existing ? existing.id : null,
                       anchor: existing ? existing.anchor : anchor };
  var input = el.querySelector('.paper-note-editor-input');
  input.focus();

  el.querySelector('.paper-note-cancel').addEventListener('click', _paperNoteCloseEditor);
  el.querySelector('.paper-note-save').addEventListener('click', async function () {
    var text = input.value.trim();
    if (!text) return;
    var view = _paperNotesView();
    var langKey = (view && typeof view.langKey === 'function') ? view.langKey() : '';
    var _g2 = (typeof window._paperXpGet === 'function') ? window._paperXpGet : null;
    var _s2 = (typeof window._paperXpSet === 'function') ? window._paperXpSet : null;
    try {
      if (_paperNoteEditor.noteId) {
        await Api.paper.notesUpdate(_paperNoteEditor.noteId, text);
        var notes = (_g2 && view) ? (_g2(view, '_paperNotes') || [])
                                  : ((view && view._paperNotes) || []);
        for (var i = 0; i < notes.length; i++) {
          if (notes[i].id === _paperNoteEditor.noteId) notes[i].note = text;
        }
      } else {
        var phash2 = (typeof _paperHash !== 'undefined') ? _paperHash : '';
        var data = await Api.paper.notesCreate({
          paper_hash: phash2, lang: langKey,
          anchor: _paperNoteEditor.anchor || {}, note: text,
        });
        if (data && data.ok && data.note && view) {
          var cur = (_g2 ? (_g2(view, '_paperNotes') || []) : (view._paperNotes || []));
          var next = cur.concat([data.note]);
          if (_s2) _s2(view, '_paperNotes', next); else view._paperNotes = next;
        }
      }
    } catch (e) {
      console.warn('[Paper:Notes] save failed:', e);
    }
    _paperNoteCloseEditor();
    _paperNotesRefresh(view);
  });
  var askBtn = el.querySelector('.paper-note-ask');
  if (askBtn) {
    askBtn.addEventListener('click', function () {
      var q = ((_paperNoteEditor.anchor || {}).quote
        ? '> ' + _paperNoteEditor.anchor.quote + '\n\n' : '') + (input.value || '');
      _paperNoteCloseEditor();
      if (typeof _paperAskQuestion === 'function') _paperAskQuestion(q);
    });
  }
  var delBtn = el.querySelector('.paper-note-del');
  if (delBtn) {
    delBtn.addEventListener('click', async function () {
      try {
        await Api.paper.notesDelete(_paperNoteEditor.noteId);
        var view2 = _paperNotesView();
        var _g3 = (typeof window._paperXpGet === 'function') ? window._paperXpGet : null;
        var _s3 = (typeof window._paperXpSet === 'function') ? window._paperXpSet : null;
        var cur2 = view2 ? ((_g3 ? _g3(view2, '_paperNotes') : view2._paperNotes) || []) : [];
        var next2 = cur2.filter(function (n) { return n.id !== _paperNoteEditor.noteId; });
        if (view2) {
          if (_s3) _s3(view2, '_paperNotes', next2); else view2._paperNotes = next2;
        }
      } catch (e) {
        console.warn('[Paper:Notes] delete failed:', e);
      }
      _paperNoteCloseEditor();
      _paperNotesRefresh(view2);
    });
  }
}

function _paperNoteCloseEditor() {
  if (_paperNoteEditor && _paperNoteEditor.el && _paperNoteEditor.el.parentNode) {
    _paperNoteEditor.el.parentNode.removeChild(_paperNoteEditor.el);
  }
  _paperNoteEditor = null;
}

/** Selection-bar entry: create a note from the current report selection. */
function _paperNoteFromSelection() {
  var anchor = _paperNoteAnchorFromSelection();
  if (!anchor) return;
  var sel = window.getSelection();
  var rect = sel && sel.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : { left: 200, bottom: 200 };
  _paperNoteOpenEditor(anchor, null, rect.left, rect.bottom);
  if (typeof _hidePaperQuoteBar === 'function') _hidePaperQuoteBar();
}

/** Re-decorate the live article after any mutation. */
function _paperNotesRefresh(view) {
  var container = document.getElementById('paperReportContent');
  var article = container && container.querySelector('.paper-report-article');
  if (article) _paperNotesDecorate(article, view);
}

/** After-render seam (called by reading_xp): load (if needed) + decorate. */
function _paperNotesAfterRender(article, container, view) {
  if (!view) return;
  var cur = (typeof window._paperXpGet === 'function')
    ? window._paperXpGet(view, '_paperNotes') : view._paperNotes;
  if (Array.isArray(cur)) {
    _paperNotesDecorate(article, view);
  } else {
    _paperNotesLoad(view).then(function () { _paperNotesDecorate(article, view); });
  }
}

if (typeof window !== 'undefined') {
  window._paperNoteFromSelection = _paperNoteFromSelection;
  window._paperNotesAfterRender = _paperNotesAfterRender;
  window._paperNotesDecorate = _paperNotesDecorate;
  window._paperNoteOpenEditor = _paperNoteOpenEditor;
  window._paperNoteAnchorFromSelection = _paperNoteAnchorFromSelection;
  // Chip / mark clicks open the editor for that note (delegation).
  if (!window._paperNotesClickWired) {
    window._paperNotesClickWired = true;
    document.addEventListener('click', function (ev) {
      var chip = ev.target && ev.target.closest
        ? ev.target.closest('.paper-note-chip, .paper-note-mark, .paper-note-tray-row') : null;
      if (!chip) return;
      var id = chip.getAttribute('data-note-id');
      var view = _paperNotesView();
      var notes = view ? (((typeof window._paperXpGet === 'function')
        ? window._paperXpGet(view, '_paperNotes') : view._paperNotes) || []) : [];
      for (var i = 0; i < notes.length; i++) {
        if (notes[i].id === id) {
          var r = chip.getBoundingClientRect();
          _paperNoteOpenEditor(notes[i].anchor || {}, notes[i], r.left, r.bottom);
          return;
        }
      }
    });
    // Esc closes the editor.
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && _paperNoteEditor) _paperNoteCloseEditor();
    });
  }
}
