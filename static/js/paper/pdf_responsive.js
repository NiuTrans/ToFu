/* ═════════════════════════════════════════════════
   paper/pdf_responsive.js — draggable split-divider + foldable/tablet
   responsive-crossing handler (Reading Mode).

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #4). A fully SELF-CONTAINED IIFE: all divider drag state + the
   matchMedia crossing wiring live in its private closure; the only global
   surface is window._paperResponsiveOnCrossing (exposed for tests). It calls
   core fns (paperFitWidth / _setPaperMobileView / _paperPdfDoc) ONLY inside
   typeof-guarded RUNTIME handlers — never at load time — and self-inits on
   DOMContentLoaded, so loading BEFORE paper-reader.js in _DEFERRED_FILES is
   safe (window-scope; no load-time cross-file read).
   ═════════════════════════════════════════════════ */

// ── Draggable Divider ──

(function() {
  var _dragging = false;
  var _startX = 0;
  var _startLeftW = 0;
  var _startRightW = 0;
  var _divider, _left, _right, _body;

  function _initDivider() {
    _divider = document.getElementById('paperDivider');
    if (!_divider) return;
    _divider.addEventListener('mousedown', _onMouseDown);
    // Touch support for tablets
    _divider.addEventListener('touchstart', _onTouchStart, { passive: false });
  }

  function _getElements() {
    _left = _divider ? _divider.previousElementSibling : null;
    _right = _divider ? _divider.nextElementSibling : null;
    _body = _divider ? _divider.parentElement : null;
  }

  function _onMouseDown(e) {
    e.preventDefault();
    _getElements();
    if (!_left || !_right || !_body) return;
    _dragging = true;
    _startX = e.clientX;
    _startLeftW = _left.getBoundingClientRect().width;
    _startRightW = _right.getBoundingClientRect().width;
    // Only set left to explicit width; right stays flex:1 to fill remaining space (prevents blank gap)
    _left.style.flex = 'none';
    _left.style.width = _startLeftW + 'px';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '250px';
    _divider.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', _onMouseMove);
    document.addEventListener('mouseup', _onMouseUp);
  }

  function _onMouseMove(e) {
    if (!_dragging) return;
    var dx = e.clientX - _startX;
    var bodyW = _body.getBoundingClientRect().width;
    var dividerW = _divider.getBoundingClientRect().width;
    var available = bodyW - dividerW;
    var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
    _left.style.width = newLeftW + 'px';
    // Right panel auto-fills via flex:1
  }

  function _onMouseUp() {
    _dragging = false;
    _divider.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.removeEventListener('mousemove', _onMouseMove);
    document.removeEventListener('mouseup', _onMouseUp);
    _autoRefitIfOverflowing();
  }

  // Touch support
  function _onTouchStart(e) {
    if (e.touches.length !== 1) return;
    e.preventDefault();
    _getElements();
    if (!_left || !_right || !_body) return;
    _dragging = true;
    _startX = e.touches[0].clientX;
    _startLeftW = _left.getBoundingClientRect().width;
    _startRightW = _right.getBoundingClientRect().width;
    _left.style.flex = 'none';
    _left.style.width = _startLeftW + 'px';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '250px';
    _divider.classList.add('dragging');
    document.addEventListener('touchmove', _onTouchMove, { passive: false });
    document.addEventListener('touchend', _onTouchEnd);
  }

  function _onTouchMove(e) {
    if (!_dragging || e.touches.length !== 1) return;
    e.preventDefault();
    var dx = e.touches[0].clientX - _startX;
    var bodyW = _body.getBoundingClientRect().width;
    var dividerW = _divider.getBoundingClientRect().width;
    var available = bodyW - dividerW;
    var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
    _left.style.width = newLeftW + 'px';
    // Right panel auto-fills via flex:1
  }

  function _onTouchEnd() {
    _dragging = false;
    _divider.classList.remove('dragging');
    document.removeEventListener('touchmove', _onTouchMove);
    document.removeEventListener('touchend', _onTouchEnd);
    _autoRefitIfOverflowing();
  }

  /** If divider drag shrank the panel enough that PDF pages now overflow
   *  horizontally, auto fit-to-width. Widening the panel preserves the
   *  user's current zoom (they get more whitespace, not a surprise re-render). */
  function _autoRefitIfOverflowing() {
    try {
      if (typeof _paperPdfDoc === 'undefined' || !_paperPdfDoc) return;
      var viewer = document.getElementById('paperPdfViewer');
      if (!viewer) return;
      var firstWrapper = viewer.querySelector('.paper-page-wrapper');
      if (!firstWrapper) return;
      var pageW = parseFloat(firstWrapper.style.width) || firstWrapper.clientWidth;
      var availW = viewer.clientWidth - 32;
      if (availW > 0 && pageW > availW + 1 && typeof paperFitWidth === 'function') {
        paperFitWidth();
      }
    } catch (err) {
      console.warn('[Paper] Auto-refit check failed:', err);
    }
  }

  // Double-click to reset to 50/50
  function _onDblClick() {
    _getElements();
    if (!_left || !_right) return;
    _left.style.flex = '1';
    _left.style.width = '';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '';
  }

  // ── Foldable / tablet responsiveness ──
  // The layout decision (side-by-side split vs single-pane + bottom switcher)
  // is made purely in CSS by this same predicate. But a fold/unfold or an
  // orientation flip changes the .paper-left width WITHOUT any JS running, so a
  // PDF laid out at the old width stays mis-sized, and a body that never had a
  // [data-paper-view] set (desktop never needs one) lands in single-pane with
  // NEITHER pane shown. On a *crossing* we therefore (a) re-assert the view —
  // defaulting to 'pdf' when entering single-pane with no view yet — and
  // (b) refit the PDF to the now-correct width. rRF-coalesced so a drag-resize
  // that fires 'change' repeatedly does the fit work at most once per frame.
  var _singlePaneMq = null;
  var _crossPending = false;

  try {
    if (typeof window.matchMedia === 'function') {
      _singlePaneMq = window.matchMedia('(max-width:1024px) and (pointer:coarse)');
    }
  } catch (e) {
    console.warn('[Paper] matchMedia unavailable:', e);
  }

  // Named + global so the responsive behaviour is directly testable / neuterable.
  function _paperResponsiveOnCrossing() {
    var body = document.querySelector('.paper-body');
    if (!body) return;
    var singlePane = !!(_singlePaneMq && _singlePaneMq.matches);
    if (singlePane) {
      // Entering (or already in) single-pane: guarantee a pane is shown.
      var cur = body.getAttribute('data-paper-view');
      if (cur !== 'pdf' && cur !== 'reader') {
        cur = 'pdf';
      }
      if (typeof _setPaperMobileView === 'function') {
        _setPaperMobileView(cur);
      } else {
        body.setAttribute('data-paper-view', cur);
      }
    }
    // The pane the PDF lives in just changed width (fold/orientation/split⇄single),
    // so a page fitted to the old width now overflows or under-fills. Refit on the
    // next frame, once the new layout has settled.
    if (typeof paperFitWidth === 'function') {
      requestAnimationFrame(function() {
        try { paperFitWidth(); } catch (err) { console.warn('[Paper] responsive fit failed:', err); }
      });
    }
  }
  window._paperResponsiveOnCrossing = _paperResponsiveOnCrossing;

  // rAF-coalesce: many rapid 'change'/'orientationchange'/'resize' events during
  // a fold collapse to a single crossing handler run per frame.
  function _scheduleCrossing() {
    if (_crossPending) return;
    _crossPending = true;
    requestAnimationFrame(function() {
      _crossPending = false;
      _paperResponsiveOnCrossing();
    });
  }

  function _wireResponsiveCrossing() {
    if (_singlePaneMq) {
      // The MediaQueryList 'change' event fires ONLY on a true predicate
      // crossing — exactly the fold/rotate boundary we care about (not every
      // resize pixel), so this is inherently debounced at the source.
      if (typeof _singlePaneMq.addEventListener === 'function') {
        _singlePaneMq.addEventListener('change', _scheduleCrossing);
      } else if (typeof _singlePaneMq.addListener === 'function') {
        _singlePaneMq.addListener(_scheduleCrossing);  // Safari <14 fallback
      }
    }
    // orientationchange isn't always a predicate crossing (portrait↔landscape
    // can stay inside single-pane), but the pane still resizes → refit anyway.
    window.addEventListener('orientationchange', _scheduleCrossing);
  }

  function _initPaperResponsive() {
    _initDivider();
    var d = document.getElementById('paperDivider');
    if (d) d.addEventListener('dblclick', _onDblClick);
    _wireResponsiveCrossing();
  }

  // Init when DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initPaperResponsive);
  } else {
    _initPaperResponsive();
  }
})();
