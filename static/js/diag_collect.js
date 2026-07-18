/* ═══════════════════════════════════════════════════════════════════
   diag_collect.js — one-click diagnostics collector

   Exposes window.__tofuCollectDiagnostics(): a Promise<string> resolving to a
   JSON blob describing the client's current state. The Android WebView shell's
   "Copy diagnostics" FAB (tofu-android WebScreen.kt) calls this via
   evaluateJavascript() and writes the result to the NATIVE clipboard, so a user
   can hand the maintainer exactly the evidence needed — even when the SPA is
   wedged on the "Fetching messages…" skeleton (the failure this was built to
   diagnose: does the conversation GET carry ?window=60, and does its body
   actually arrive over the /proxy/…/ tunnel?).

   HARD RULE: this must NEVER throw and NEVER depend on app state being healthy.
   Every field is guarded; a partial blob is more useful than an exception. It
   is READ-ONLY except for one deliberate, harmless live GET probe of the active
   conversation (same endpoint the app already calls on open).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  function _safe(fn, dflt) {
    try { return fn(); } catch (_) { return dflt; }
  }

  /* Snapshot the active conversation's in-memory load state — the fields that
   * decide whether the skeleton clears (_needsLoad) and whether the load was
   * windowed (_windowed) or a full-blob transfer. */
  function _activeConvSnapshot() {
    return _safe(function () {
      var id = (typeof activeConvId !== 'undefined') ? activeConvId : null;
      if (!id || typeof conversations === 'undefined') return { activeConvId: id };
      var c = conversations.find(function (x) { return x && x.id === id; });
      if (!c) return { activeConvId: id, found: false };
      return {
        activeConvId: id,
        found: true,
        needsLoad: !!c._needsLoad,
        inMemoryMsgCount: (c.messages && c.messages.length) || 0,
        serverMsgCount: c._serverMsgCount || 0,
        windowed: !!c._windowed,
        trimmed: !!c._trimmed,
        hasMoreEarlier: !!c._hasMoreEarlier,
        totalCount: c._totalCount || null,
      };
    }, { error: 'activeConv snapshot failed' });
  }

  /* Is the loading skeleton ("Fetching … messages from server") currently on
   * screen? That is the exact stuck state the user reports. */
  function _skeletonShowing() {
    return _safe(function () {
      var inner = document.getElementById('chatInner');
      if (!inner) return null;
      var txt = (inner.textContent || '');
      return txt.indexOf('Fetching') !== -1 && txt.indexOf('from server') !== -1;
    }, null);
  }

  /* The windowing policy this build would apply to a first-open GET. */
  function _windowConfig() {
    return _safe(function () {
      var param = (typeof convWindowParam === 'function') ? convWindowParam() : '(fn missing)';
      var override = (typeof window !== 'undefined') ? window.TOFU_CONV_WINDOW : undefined;
      return { windowParam: param, override: (override === undefined ? null : override) };
    }, { error: 'window config failed' });
  }

  /* LIVE PROBE — the decisive test for issue #1. Re-fetch the active
   * conversation exactly as the app does on open (with the ?window= param),
   * with a hard client timeout, and report: did the body arrive, how big was
   * it, how long did it take, and did the server mark it windowed? A body that
   * never arrives (timeout) with the server-side log showing a fast 200 proves
   * the tunnel is buffering/truncating the response. */
  function _liveGetProbe() {
    return new Promise(function (resolve) {
      var id = _safe(function () { return (typeof activeConvId !== 'undefined') ? activeConvId : null; }, null);
      if (!id) { resolve({ skipped: 'no active conversation' }); return; }
      var param = _safe(function () {
        return (typeof convWindowParam === 'function') ? convWindowParam() : '';
      }, '');
      var base = _safe(function () {
        return (typeof BASE_PATH !== 'undefined' && BASE_PATH) ? BASE_PATH : '';
      }, '');
      var url = base + '/api/v1/conversations/' + encodeURIComponent(id)
        + (param ? ('?window=' + encodeURIComponent(param)) : '');
      var t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      var ctrl = _safe(function () { return new AbortController(); }, null);
      var timer = setTimeout(function () { _safe(function () { ctrl && ctrl.abort(); }); }, 15000);
      var opts = { headers: { 'Accept': 'application/json' } };
      if (ctrl) opts.signal = ctrl.signal;
      fetch(url, opts).then(function (resp) {
        return resp.text().then(function (body) {
          clearTimeout(timer);
          var elapsedMs = Math.round((typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0);
          var windowedFlag = null, totalCount = null, msgLen = null, parseErr = null;
          try {
            var j = JSON.parse(body);
            windowedFlag = (j && j.windowed === true);
            totalCount = (j && j.totalCount) || null;
            msgLen = (j && j.messages && j.messages.length) || 0;
          } catch (e) { parseErr = String(e && e.message || e); }
          resolve({
            requestedWith: param ? ('window=' + param) : '(no window param — full blob)',
            httpStatus: resp.status,
            bodyBytes: body.length,
            elapsedMs: elapsedMs,
            serverSaysWindowed: windowedFlag,
            totalCount: totalCount,
            messagesReturned: msgLen,
            jsonParseError: parseErr,
          });
        });
      }).catch(function (e) {
        clearTimeout(timer);
        var elapsedMs = Math.round((typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0);
        var aborted = (e && (e.name === 'AbortError'));
        resolve({
          requestedWith: param ? ('window=' + param) : '(no window param — full blob)',
          failed: true,
          aborted: aborted,
          note: aborted
            ? 'fetch aborted at 15s — body never fully arrived (tunnel buffering / truncation suspected)'
            : ('fetch error: ' + String(e && e.message || e)),
          elapsedMs: elapsedMs,
        });
      });
    });
  }

  window.__tofuCollectDiagnostics = function () {
    var blob = {
      collectedAt: new Date().toISOString(),
      note: 'Tofu client diagnostics — paste this to the maintainer.',
      location: _safe(function () { return location.href; }, null),
      userAgent: _safe(function () { return navigator.userAgent; }, null),
      viewport: _safe(function () {
        return {
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight,
          dpr: window.devicePixelRatio,
          vh100: document.documentElement.style.getPropertyValue('--vh100') || '(unset)',
        };
      }, null),
      bundle: _safe(function () {
        var s = document.querySelector('script[src*="bundle-"]');
        return s ? (s.getAttribute('src') || '').replace(/^.*\//, '') : '(dev, unbundled)';
      }, null),
      conversationCount: _safe(function () {
        return (typeof conversations !== 'undefined') ? conversations.length : null;
      }, null),
      windowConfig: _windowConfig(),
      skeletonShowing: _skeletonShowing(),
      activeConv: _activeConvSnapshot(),
      recentLog: _safe(function () { return (window.__tofuDiagRing || []).slice(-60); }, []),
    };
    return _liveGetProbe().then(function (probe) {
      blob.liveGetProbe = probe;
      return JSON.stringify(blob, null, 2);
    }, function () {
      blob.liveGetProbe = { error: 'probe promise rejected' };
      return JSON.stringify(blob, null, 2);
    });
  };
})();
