/* ═══════════════════════════════════════════════════════════════════
   paper/reader_prefs.js — Reader comfort preferences (text-size + width)

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #1) to begin decomposing the 5919-line paper-reader monolith into
   cohesive siblings. This is a self-contained LEAF cluster: its constants
   + five functions reference only each other; the sole external caller is
   a single RUNTIME `_applyReaderPrefs()` in enterPaperMode (paper-reader.js).

   Loads in _DEFERRED_FILES BEFORE paper-reader.js (window-scope siblings, no
   module system). All state is `var` on window — no top-level let/const, so
   there is no load-time cross-file read hazard.
   ═══════════════════════════════════════════════════════════════════ */

// ── Reader comfort preferences ─────────────────────────────────────────────
// Text-size + reading-width are GLOBAL (apply across all papers, unlike the
// per-paper language) and persist to localStorage so a reader's comfort setting
// survives reload. They drive two CSS custom properties on the reader
// containers (--reader-font-scale, --reader-measure) — pure-variable, no
// per-element rewrite, and all three themes inherit them.
var _PAPER_READER_PREFS_KEY = 'paper_reader_prefs';
// Discrete font-scale steps (index into this array is persisted). 1.0 = today.
var _READER_FONT_SCALES = [0.85, 0.925, 1.0, 1.1, 1.2, 1.3];
var _READER_DEFAULT_SCALE_IDX = 2;   // → 1.0
// Reading-width presets: {measure px, i18n label key}. Index persisted.
var _READER_WIDTHS = [
  { px: 640, label: 'paper.readerWidthNarrow' },
  { px: 720, label: 'paper.readerWidthComfortable' },
  { px: 860, label: 'paper.readerWidthWide' },
];
var _READER_DEFAULT_WIDTH_IDX = 1;   // → 720 (Comfortable), today's default

/** Read persisted reader prefs, clamped to valid indices. Never throws. */
function _readReaderPrefs() {
  var scaleIdx = _READER_DEFAULT_SCALE_IDX, widthIdx = _READER_DEFAULT_WIDTH_IDX;
  try {
    var raw = localStorage.getItem(_PAPER_READER_PREFS_KEY);
    if (raw) {
      var o = JSON.parse(raw) || {};
      if (typeof o.scaleIdx === 'number') scaleIdx = o.scaleIdx;
      if (typeof o.widthIdx === 'number') widthIdx = o.widthIdx;
    }
  } catch (e) {
    console.warn('[Paper:Reader] read prefs failed:', e);
  }
  scaleIdx = Math.max(0, Math.min(_READER_FONT_SCALES.length - 1, scaleIdx | 0));
  widthIdx = Math.max(0, Math.min(_READER_WIDTHS.length - 1, widthIdx | 0));
  return { scaleIdx: scaleIdx, widthIdx: widthIdx };
}

/** Persist reader prefs (merged onto whatever is stored). Never throws. */
function _persistReaderPrefs(prefs) {
  try {
    localStorage.setItem(_PAPER_READER_PREFS_KEY, JSON.stringify({
      scaleIdx: prefs.scaleIdx, widthIdx: prefs.widthIdx,
    }));
  } catch (e) {
    console.warn('[Paper:Reader] persist prefs failed:', e);
  }
}

/** Apply the current (or given) prefs to BOTH reader containers by setting the
 *  two CSS custom properties, and sync the toolbar width label + A− disabled
 *  state. Idempotent; safe to call whenever the reader opens or a pref changes. */
function _applyReaderPrefs(prefs) {
  prefs = prefs || _readReaderPrefs();
  var scale = _READER_FONT_SCALES[prefs.scaleIdx];
  var width = _READER_WIDTHS[prefs.widthIdx];
  ['paperReportContent', 'paperReviewContent'].forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.style.setProperty('--reader-font-scale', String(scale));
    el.style.setProperty('--reader-measure', width.px + 'px');
  });
  // Sync every width-label + disable the extremes' step buttons for clarity.
  var labelText = (typeof t === 'function') ? t(width.label) : width.label;
  document.querySelectorAll('.paper-reader-width-label').forEach(function(sp) {
    sp.textContent = labelText;
  });
  document.querySelectorAll('.paper-reader-set-dec').forEach(function(b) {
    b.disabled = (prefs.scaleIdx <= 0);
  });
  return prefs;
}

/** Nudge the reading text size by ±1 step, persist, re-apply. */
function _readerFontStep(dir) {
  var prefs = _readReaderPrefs();
  var next = Math.max(0, Math.min(_READER_FONT_SCALES.length - 1, prefs.scaleIdx + (dir > 0 ? 1 : -1)));
  if (next === prefs.scaleIdx) return;
  prefs.scaleIdx = next;
  _persistReaderPrefs(prefs);
  _applyReaderPrefs(prefs);
}

/** Cycle Narrow → Comfortable → Wide → Narrow, persist, re-apply. */
function _readerWidthCycle() {
  var prefs = _readReaderPrefs();
  prefs.widthIdx = (prefs.widthIdx + 1) % _READER_WIDTHS.length;
  _persistReaderPrefs(prefs);
  _applyReaderPrefs(prefs);
}
if (typeof window !== 'undefined') {
  window._readerFontStep = _readerFontStep;
  window._readerWidthCycle = _readerWidthCycle;
  window._applyReaderPrefs = _applyReaderPrefs;
}
