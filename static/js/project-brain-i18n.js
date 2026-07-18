/* ═══════════════════════════════════════════════════════════════════
   project-brain-i18n.js — content translation as a DISPLAY OVERLAY.

   The Project Brain chrome (tab labels, buttons, lane names) already
   tracks the UI language via t('projectBrain.*'). This module handles the
   remaining, harder half: the agent/human-AUTHORED free-text CONTENT —
   charter north-star + committed decisions, board epic titles, activity
   summaries, peer-message notes — which render verbatim in whatever
   language they were written (a mix of EN charter + ZH feed lines).

   DESIGN — a view laid OVER the original, never a mutation:
     • The ORIGINAL is always the source of truth. Each translatable node
       is stamped `data-pb-src="<original>"`; the translation is written
       into the node's innerHTML as a VIEW, the original stays retrievable
       (hover title = source, and toggling the panel OFF restores it byte-
       for-byte). The commit / reject buttons act on their OWN `data-text`
       (the original proposal), which this module NEVER touches — so a
       machine translation can never drift a committed governance decision.
     • Reuses the existing WHOLE-DOCUMENT translate engine (Api.translate,
       sync `run` = no DB side-effect) — one small call per item, in
       parallel through a bounded pool. The engine already carries the
       mixed EN+ZH wrong-language-flip guard; we add a cheap LOCAL CJK
       ratio gate so an English charter on an English UI (and a Chinese
       line on a Chinese UI) does ZERO work and ZERO cost.
     • Anti-flicker via the patterns this project trusts: render the
       original immediately (the render fns are unchanged), then
       compare-before-swap IN PLACE (touch a node only when the translated
       string differs from what's shown), rAF-coalesced. Results cache by
       (hash(text), targetLang) in memory + IndexedDB, so a language re-flip
       is instant and offline. Only VISIBLE items (active tab / influence
       banner) are processed. Re-runs on tab switch + language switch.

   Toggle: a Project-Brain-SCOPED preference (localStorage tofu_pb_translate),
   default OFF, decoupled from the chat `autoTranslate` flag (different
   surface, different intent).

   Bundled by lib/js_bundler.py (_BUNDLE_FILES), after project-brain.js.
   ═══════════════════════════════════════════════════════════════════ */

var ProjectBrainI18n = (function () {
  'use strict';

  var PREF_KEY = 'tofu_pb_translate';   // '1' | '0' (PB-scoped, default ON)
  var POOL_LIMIT = 6;                   // max concurrent translate requests
  var _memCache = Object.create(null);  // "hash|lang" → translated string

  // ── Preference (Project-Brain-scoped, NOT the chat autoTranslate flag) ──
  // Default ON (opt-out): the Project Brain content (charter north-star +
  // committed decisions, board titles, activity/peer notes) is agent-authored
  // and often in a different language than the UI. Showing it in the UI
  // language by default is the expected behaviour; the already-target CJK gate
  // keeps a same-language surface at ZERO cost, so default-on is free when the
  // content already matches the UI language. Only an explicit '0' disables it.
  function isEnabled() {
    try { return localStorage.getItem(PREF_KEY) !== '0'; }
    catch (_e) { return true; }
  }
  function _setEnabled(on) {
    try { localStorage.setItem(PREF_KEY, on ? '1' : '0'); }
    catch (_e) { /* localStorage unavailable — session-only */ }
  }

  // ── Language / already-target gate ──────────────────────────────
  /** UI language → the translate engine's target vocabulary. */
  function targetLang() {
    var lang = (typeof _i18nLang !== 'undefined' && _i18nLang) ? _i18nLang : 'zh';
    return lang === 'zh' ? 'Chinese' : 'English';
  }

  /** Cheap LOCAL CJK ratio (mirrors the _TRANSLATE_SKIP_CJK_RATIO policy). */
  function _cjkRatio(s) {
    var t = String(s == null ? '' : s).replace(/\s+/g, '');
    if (!t) return 0;
    var m = t.match(/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff]/g);
    return m ? m.length / t.length : 0;
  }

  /**
   * True when `text` is ALREADY in `target` so a translation pass would be a
   * no-op — the "zero work, zero cost" gate. Chinese target: skip when the
   * source is already predominantly CJK (≥0.30). English target: skip when
   * the source is already predominantly non-CJK (ratio <0.10). This makes an
   * EN charter on an EN UI and a ZH line on a ZH UI free.
   */
  function _alreadyTarget(text, target) {
    var r = _cjkRatio(text);
    if (target === 'Chinese') return r >= 0.30;
    return r < 0.10;
  }

  // ── FNV-1a 32-bit hash (cache key; collision-tolerant) ──────────
  function _hash32(str) {
    var h = 0x811c9dc5;
    str = String(str == null ? '' : str);
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h.toString(36);
  }
  // Cache-key version salt. Bump when a translation-quality fix could make a
  // PREVIOUSLY-cached entry wrong (e.g. v2: truncated bodies are no longer
  // cached — old entries may hold a mid-sentence partial). Bumping it makes
  // every prior (src,target) key miss so the stale partial is never served
  // again; a fresh (now complete) translation replaces it under the new key.
  var _CACHE_VER = 'v2';
  function _cacheKey(src, target) {
    return _hash32(src) + '|' + target + '|' + _CACHE_VER;
  }

  function _esc(s) {
    return escapeHtml(String(s == null ? '' : s));
  }

  // ── IndexedDB persistence (best-effort, fail-open) ──────────────
  var DB_NAME = 'tofu_pb_translate';
  var STORE = 'tr';
  var _db = null, _dbP = null, _idbAvail = true;

  function _openDB() {
    if (_dbP) return _dbP;
    if (!_idbAvail) return Promise.resolve(null);
    _dbP = new Promise(function (resolve) {
      try {
        if (typeof indexedDB === 'undefined') { _idbAvail = false; resolve(null); return; }
        var req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = function (e) {
          var db = e.target.result;
          if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'k' });
        };
        req.onsuccess = function (e) { _db = e.target.result; resolve(_db); };
        req.onerror = function () {
          if (typeof console !== 'undefined') console.warn('[PB-i18n] IDB open failed');
          _idbAvail = false; resolve(null);
        };
      } catch (err) {
        if (typeof console !== 'undefined') console.warn('[PB-i18n] IDB init error:', err && err.message);
        _idbAvail = false; resolve(null);
      }
    });
    return _dbP;
  }

  function _idbGet(key) {
    return _openDB().then(function (db) {
      if (!db) return null;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readonly');
          var r = tx.objectStore(STORE).get(key);
          r.onsuccess = function () { resolve(r.result ? r.result.v : null); };
          r.onerror = function () { resolve(null); };
        } catch (_e) { resolve(null); }
      });
    });
  }

  function _idbPut(key, val) {
    return _openDB().then(function (db) {
      if (!db) return;
      try {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put({ k: key, v: val, t: Date.now() });
      } catch (e) {
        if (typeof console !== 'undefined') console.debug('[PB-i18n] IDB put skipped:', e && e.message);
      }
    });
  }

  // ── rAF-coalesced compare-before-swap ───────────────────────────
  var _pendingSwaps = [];
  var _rafScheduled = false;

  function _scheduleSwap(el, src, translated) {
    _pendingSwaps.push({ el: el, src: src, translated: translated });
    if (_rafScheduled) return;
    _rafScheduled = true;
    var raf = (typeof requestAnimationFrame === 'function')
      ? requestAnimationFrame : function (f) { return setTimeout(f, 16); };
    raf(function () {
      _rafScheduled = false;
      var batch = _pendingSwaps; _pendingSwaps = [];
      for (var i = 0; i < batch.length; i++) _applySwap(batch[i]);
    });
  }

  /** Overlay one translation. Compare-before-swap: no DOM write when the
   *  shown text already matches (defeats flicker on re-apply / re-render). */
  function _applySwap(item) {
    var el = item.el;
    if (!el || !el.getAttribute) return;
    // The node may have been re-rendered (fresh element) or its source
    // changed since the request was issued — guard against a stale swap.
    if (el.getAttribute('data-pb-src') !== item.src) return;
    if (el._pbShown === item.translated) return;           // already shown
    el.innerHTML = _esc(item.translated);
    el.title = item.src;                                   // hover reveals original
    el.setAttribute('data-pb-tr', '1');
    el._pbShown = item.translated;
  }

  /** Restore a node to its ORIGINAL (source) content. Compare-before-swap. */
  function _revert(el) {
    if (!el || !el.getAttribute) return;
    if (!el.getAttribute('data-pb-tr') && el._pbShown === undefined) return;
    var src = el.getAttribute('data-pb-src') || '';
    if (el._pbShown !== null && el._pbShown !== undefined) {
      el.innerHTML = _esc(src);
    }
    el.removeAttribute('data-pb-tr');
    if (el.title) el.title = '';
    el._pbShown = null;
  }

  // ── Bounded-concurrency fetch pool ──────────────────────────────
  function _runPool(items, worker, limit) {
    limit = limit || POOL_LIMIT;
    return new Promise(function (resolve) {
      var i = 0, active = 0, done = false;
      function step() {
        if (done) return;
        if (i >= items.length && active === 0) { done = true; resolve(); return; }
        while (active < limit && i < items.length) {
          var it = items[i++]; active++;
          Promise.resolve(worker(it)).catch(function () {}).then(function () {
            active--; step();
          });
        }
      }
      step();
    });
  }

  /** Translate one source string via the sync engine (NO convId → no DB
   *  side-effect; pure display overlay). Returns translated | null. */
  function _translateOne(src, target) {
    var api = (typeof Api !== 'undefined' && Api.translate) ? Api.translate : null;
    if (!api || typeof api.run !== 'function') return Promise.resolve(null);
    return Promise.resolve(
      api.run({ text: src, targetLang: target, sourceLang: '' }, { onError: 'null' })
    ).then(function (d) {
      // Refuse a KNOWN-incomplete translation: the engine flags a body it had
      // to accept mid-truncation with `truncated:true`. Showing it would
      // REPLACE the complete original (preserved in data-pb-src) with a
      // cut-off view — the reported "displayed incompletely" bug. Returning
      // null keeps the full original visible (in its source language) and
      // skips the IDB cache so a later pass can re-translate completely.
      if (d && d._ok && d.translated && !d.truncated) return d.translated;
      if (d && d.truncated && typeof console !== 'undefined') {
        console.warn('[PB-i18n] dropping truncated translation, keeping original');
      }
      return null;
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[PB-i18n] translate failed:', e && e.message);
      return null;
    });
  }

  // ── Visibility gate — only translate what's on screen ───────────
  /** A node is "visible" when it's in the active tab panel (or not inside a
   *  tab panel at all, e.g. the influence banner). Layout-free (class-based)
   *  so it's deterministic under jsdom. */
  function _isVisible(el) {
    if (!el || !el.closest) return true;
    var panel = el.closest('.pb-tab-panel');
    if (!panel) return true;
    return panel.classList.contains('pb-tab-panel-active');
  }

  /** Lazy-on-expand: a long-text clamp (`.pb-clamp`) is visually COLLAPSED
   *  until the user expands it (`.pb-clamp-open`). Defer translating a
   *  collapsed clamp so we don't spend a call on a wall of text nobody
   *  opened; short text (`.pb-clamp-inner`) is never deferred. The clamp
   *  toggle re-invokes apply() on expand (project-brain.js _wireClampToggles). */
  function _deferUntilExpand(el) {
    if (!el || !el.classList) return false;
    if (!el.classList.contains('pb-clamp')) return false;   // short text or non-clamp
    return !el.classList.contains('pb-clamp-open');          // collapsed → defer
  }

  // ── Core: apply translations to a rendered subtree ──────────────
  /**
   * Overlay translations onto every `[data-pb-src]` node under `root`.
   * Renders nothing itself — the render functions already painted the
   * originals; this lays the translation over them. Idempotent + cheap on
   * re-run (cache hits → rAF compare-before-swap; already-target → skip).
   */
  function apply(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    var nodes = root.querySelectorAll('[data-pb-src]');
    // Also consider `root` itself when it carries the marker.
    var self = (root.getAttribute && root.getAttribute('data-pb-src') != null) ? [root] : [];
    var all = self.concat(Array.prototype.slice.call(nodes));
    if (!all.length) return;

    var enabled = isEnabled();
    var target = targetLang();
    var byKey = Object.create(null);   // key → {src, els:[]}
    var needIdb = [];

    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      var src = el.getAttribute('data-pb-src') || '';
      if (!enabled || !src || _alreadyTarget(src, target) || !_isVisible(el)) {
        // Toggle off, empty, already-in-target, or off-screen → show original.
        _revert(el);
        continue;
      }
      // Lazy: a collapsed long-text clamp waits until the user expands it.
      // Leave it showing the original (a partial fragment) until then.
      if (_deferUntilExpand(el)) { _revert(el); continue; }
      var key = _cacheKey(src, target);
      var cached = _memCache[key];
      if (cached != null) { _scheduleSwap(el, src, cached); continue; }
      if (!byKey[key]) { byKey[key] = { src: src, els: [] }; needIdb.push(key); }
      byKey[key].els.push(el);
    }

    if (!needIdb.length) return;

    // Resolve IDB hits first (a re-flip is instant/offline), then fetch misses.
    Promise.all(needIdb.map(function (key) {
      return _idbGet(key).then(function (v) {
        if (v != null) {
          _memCache[key] = v;
          var grp = byKey[key];
          for (var j = 0; j < grp.els.length; j++) _scheduleSwap(grp.els[j], grp.src, v);
          return null;   // resolved from cache — no fetch needed
        }
        return key;      // still a miss → fetch
      });
    })).then(function (missKeys) {
      var toFetch = missKeys.filter(function (k) { return !!k; });
      if (!toFetch.length) return;
      return _runPool(toFetch, function (key) {
        var grp = byKey[key];
        return _translateOne(grp.src, target).then(function (translated) {
          if (translated == null) return;
          _memCache[key] = translated;
          _idbPut(key, translated);
          for (var j = 0; j < grp.els.length; j++) _scheduleSwap(grp.els[j], grp.src, translated);
        });
      });
    });
  }

  /** Re-scan + re-apply across the whole open panel + influence banner.
   *  Called on toggle, language switch, and tab switch. */
  function applyAll() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay && !overlay.hidden) apply(overlay);
    var banner = document.getElementById('projectBrainInfluence');
    if (banner && !banner.hidden) apply(banner);
  }

  // ── Toggle button wiring (Project-Brain head) ───────────────────
  function _syncToggleBtn() {
    var btn = document.getElementById('projectBrainTranslateToggle');
    if (!btn) return;
    var on = isEnabled();
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.classList.toggle('pb-tr-toggle-on', on);
    var ico = btn.querySelector('.pb-tr-toggle-ico');
    if (ico && !ico.innerHTML && typeof Icon === 'function') {
      ico.innerHTML = Icon('languages', 14);
    }
    var lbl = btn.querySelector('.pb-tr-toggle-label');
    if (lbl && typeof t === 'function') lbl.textContent = t('projectBrain.translateToggle');
    if (typeof t === 'function') btn.title = t('projectBrain.translateToggleTitle');
  }

  /** Wire the head toggle once (idempotent). Called from openProjectBrain. */
  function initToggle() {
    var btn = document.getElementById('projectBrainTranslateToggle');
    if (!btn) return;
    if (!btn._pbTrWired) {
      btn.addEventListener('click', toggle);
      btn._pbTrWired = true;
    }
    _syncToggleBtn();
  }

  function toggle() {
    _setEnabled(!isEnabled());
    _syncToggleBtn();
    applyAll();
  }

  // Expose for the render call-sites + the jsdom harness.
  return {
    isEnabled: isEnabled,
    toggle: toggle,
    initToggle: initToggle,
    targetLang: targetLang,
    apply: apply,
    applyAll: applyAll,
    // Testing seams:
    _alreadyTarget: _alreadyTarget,
    _cacheKey: _cacheKey,
    _revert: _revert,
    _memCache: _memCache,
  };
})();

if (typeof window !== 'undefined') window.ProjectBrainI18n = ProjectBrainI18n;
