/* ===== migrated source: diag_collect.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   diag_collect.js — one-click diagnostics collector

   Exposes runtimeScope.__tofuCollectDiagnostics(): a Promise<string> resolving to a
   JSON blob describing the client's current state. The Android WebView shell's
   "Copy diagnostics" FAB (tofu-android WebScreen.kt) calls this via
   evaluateJavascript() and writes the result to the NATIVE clipboard, so a user
   can hand the maintainer exactly the evidence needed when the SPA is wedged.

   HARD RULE: this must NEVER throw and NEVER depend on app state being healthy.
   Every field is guarded; a partial blob is more useful than an exception. It
   is strictly read-only and never creates a second transcript request.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  function _safe(fn, dflt) {
    try { return fn(); } catch (_) { return dflt; }
  }

  /* Snapshot the catalog shell and its authoritative TurnState read model. */
  function _activeConvSnapshot() {
    return _safe(function () {
      var id = (typeof activeConvId !== 'undefined') ? activeConvId : null;
      if (!id || typeof conversations === 'undefined') return { activeConvId: id };
      var c = conversations.find(function (x) { return x && x.id === id; });
      if (!c) return { activeConvId: id, found: false };
      var state = runtimeScope.ConversationTurnRead?.state?.(c);
      return {
        activeConvId: id,
        found: true,
        turnSnapshotRequired: !!c._turnSnapshotRequired,
        inMemoryTurnCount: runtimeScope.ConversationTurnRead?.ordered?.(c)?.length || 0,
        serverTurnCount: c._serverTurnCount || 0,
        revision: state?.conversationRevision || 0,
        transport: state?.transport || 'unavailable',
        activeAttemptCount:
          runtimeScope.ConversationTurnRead?.activeAttemptIds?.(c)?.length || 0,
        livePhase: state?.livePhase || null,
      };
    }, { error: 'activeConv snapshot failed' });
  }

  function _surfaceSnapshot() {
    return _safe(function () {
      var surface = document.querySelector(
        '[data-conversation-surface="turn-store"]',
      );
      if (!surface) return null;
      return {
        conversationId: surface.dataset.conversationId || null,
        revision: Number(surface.dataset.conversationRevision || 0),
        transport: surface.dataset.transport || null,
        turnNodeCount: surface.querySelectorAll('[data-turn-id]').length,
      };
    }, null);
  }

  function _conversationSyncConfig() {
    return {
      protocol: 'conversation-sync-v3',
      authority: 'sidecar-turn-store',
      browserTranscriptCache: 'none',
    };
  }

  function _liveStateProbe() {
    return _safe(function () {
      var id = (typeof activeConvId !== 'undefined') ? activeConvId : null;
      if (!id) return { skipped: 'no active conversation' };
      var state = runtimeScope.ConversationTurnRead?.state?.(id);
      if (!state) return {
        protocol: 'conversation-sync-v3',
        conversationId: id,
        skipped: 'TurnStore state unavailable',
      };
      return {
        protocol: 'conversation-sync-v3',
        conversationId: id,
        revision: state.conversationRevision || 0,
        transport: state.transport || 'unavailable',
        turnCount: runtimeScope.ConversationTurnRead?.ordered?.(id)?.length || 0,
        activeAttemptCount:
          runtimeScope.ConversationTurnRead?.activeAttemptIds?.(id)?.length || 0,
        livePhase: state.livePhase || null,
      };
    }, { error: 'live TurnStore probe failed' });
  }

  function _collectDiagnosticsLegacy() {
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
      conversationSync: _conversationSyncConfig(),
      surface: _surfaceSnapshot(),
      activeConv: _activeConvSnapshot(),
      recentLog: _safe(function () { return (runtimeScope.__tofuDiagRing || []).slice(-60); }, []),
    };
    blob.liveStateProbe = _liveStateProbe();
    return Promise.resolve(JSON.stringify(blob, null, 2));
  }

  /* Prefer the typed, independently cached Vite chunk when it is available.
   * Keep the full collector above as rollback authority: stale manifests,
   * blocked module requests, and older Android WebViews must still produce the
   * diagnostic blob precisely when the rest of the SPA is unhealthy. */
  runtimeScope.__tofuCollectDiagnostics = function () {
    var modern = _safe(function () {
      return window.TofuModules && window.TofuModules.collectDiagnostics;
    }, null);
    if (typeof modern === 'function') {
      try {
        return Promise.resolve(modern.call(window.TofuModules)).then(
          function (value) { return value; },
          function () { return _collectDiagnosticsLegacy(); }
        );
      } catch (_) {
        return _collectDiagnosticsLegacy();
      }
    }
    return _collectDiagnosticsLegacy();
  };
})();
