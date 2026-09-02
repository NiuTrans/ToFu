/* ===== migrated source: idb-cache.js ===== */
// ══════════════════════════════════════════════════════
//  idb-cache.js — IndexedDB metadata cache for the conversation catalog
//
//  Architecture:
//    - Server (PostgreSQL) is the SINGLE SOURCE OF TRUTH
//    - IndexedDB stores catalog/settings metadata only, never transcripts
//    - TurnStore hydrates every transcript from Conversation Sync v3
//    - On failure: graceful fallback to server fetch (cache is optional)
//
//  Schema v4:
//    conv_meta    — locally observed catalog metadata/settings, LRU by cachedAt.
//    sidebar_meta — last complete server catalog for instant first paint.
//  The former messages store is deleted during upgrade because retaining a
//  second transcript document recreated the state-divergence class TurnStore
//  was introduced to eliminate.
// ══════════════════════════════════════════════════════

var ConvCache = (function () {
  'use strict';

  var DB_NAME = 'tofu_conv_cache';
  var LEGACY_DB_NAME = 'chatui_conv_cache';
  var DB_VERSION = 4;
  var META_STORE = 'conv_meta';
  var LEGACY_MSG_STORE = 'messages';
  // v3: a SEPARATE lightweight store holding the LAST FULL server sidebar list
  // (id/title/updatedAt/rev/msgCount/settings only — no message bodies). This
  // is DISTINCT from META_STORE, whose invariant is one metadata/settings row
  // per locally observed conversation. sidebar_meta lets the
  // boot path paint the ENTIRE sidebar (not just opened convs) before the
  // server round-trip. Rewritten wholesale on every successful full load so it
  // stays an accurate mirror (a conv deleted elsewhere falls out naturally).
  var SIDEBAR_STORE = 'sidebar_meta';
  var LEGACY_V1_STORE = 'conversations';
  var MAX_CACHED = 200;
  // Trigger eviction every N puts so long-lived tabs don't grow past MAX_CACHED.
  var EVICT_EVERY_N_PUTS = 20;
  // Quota check cadence — once per session is plenty; estimate() is non-trivial.
  var QUOTA_CHECK_INTERVAL_MS = 5 * 60 * 1000;

  // One-time best-effort cleanup of the legacy IndexedDB.  Server
  // (PostgreSQL/SQLite) is the source of truth, so dropping the cache
  // costs one extra server fetch per conversation the first time it's
  // re-opened post-rollout — acceptable transient blip.  Guarded by a
  // localStorage flag so we don't fire a deleteDatabase request on every
  // page load forever.
  var LEGACY_CLEANUP_FLAG = 'tofu_conv_cache_legacy_cleanup_v1';
  try {
    var alreadyCleaned = false;
    try { alreadyCleaned = localStorage.getItem(LEGACY_CLEANUP_FLAG) === '1'; } catch (_e) { /* localStorage unavailable */ }
    if (!alreadyCleaned && typeof indexedDB !== 'undefined' && typeof indexedDB.deleteDatabase === 'function') {
      indexedDB.deleteDatabase(LEGACY_DB_NAME);
      try { localStorage.setItem(LEGACY_CLEANUP_FLAG, '1'); } catch (_e) { /* ignore */ }
    }
  } catch (_e) { /* no-op — legacy DB may not exist */ }

  /** @type {IDBDatabase|null} */
  var _db = null;
  /** @type {Promise<IDBDatabase>|null} */
  var _dbPromise = null;
  var _available = true;  // false if IndexedDB is unavailable or errored
  var _putsSinceEvict = 0;
  var _lastQuotaCheck = 0;

  // ── Open / Init ──

  function _open() {
    if (_dbPromise) return _dbPromise;
    if (!_available) return Promise.resolve(null);

    _dbPromise = new Promise(function (resolve) {
      if (typeof indexedDB === 'undefined') {
        console.warn('[ConvCache] IndexedDB not available');
        _available = false;
        resolve(null);
        return;
      }
      try {
        var req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = function (e) {
          var db = e.target.result;
          // v1 → v2: drop the old monolithic store (server re-fills on
          // next click — same one-shot strategy we used for the legacy
          // chatui_conv_cache DB).
          if (db.objectStoreNames.contains(LEGACY_V1_STORE)) {
            db.deleteObjectStore(LEGACY_V1_STORE);
            console.info('[ConvCache] Schema v1 → v2: dropped legacy "%s" store', LEGACY_V1_STORE);
          }
          if (!db.objectStoreNames.contains(META_STORE)) {
            var meta = db.createObjectStore(META_STORE, { keyPath: 'id' });
            meta.createIndex('cachedAt', 'cachedAt', { unique: false });
          }
          if (db.objectStoreNames.contains(LEGACY_MSG_STORE)) {
            db.deleteObjectStore(LEGACY_MSG_STORE);
          }
          // v2 → v3: full-list sidebar mirror (lightweight, keyed by id).
          if (!db.objectStoreNames.contains(SIDEBAR_STORE)) {
            db.createObjectStore(SIDEBAR_STORE, { keyPath: 'id' });
          }
        };
        req.onsuccess = function (e) {
          _db = e.target.result;
          _db.onclose = function () {
            console.warn('[ConvCache] DB unexpectedly closed');
            _db = null;
            _dbPromise = null;
          };
          resolve(_db);
        };
        req.onerror = function (e) {
          console.warn('[ConvCache] Failed to open DB:', e.target.error);
          _available = false;
          resolve(null);
        };
        req.onblocked = function () {
          console.warn('[ConvCache] DB open blocked — another tab has an older version open');
          _available = false;
          resolve(null);
        };
      } catch (err) {
        console.warn('[ConvCache] IndexedDB init error:', err.message);
        _available = false;
        resolve(null);
      }
    });
    return _dbPromise;
  }

  // ── Internal helpers ──

  // Settings whitelist — same fields the v1 record persisted.
  //
  // The model field MUST mirror the READER's resolution
  //   (_applySettingsToConv: `settings.model || settings.preset ||
  //   settings.effort`). Persisting only the flat `conv.model` cached a
  //   model-LESS record for any conv carrying just `preset`/`effort`; the
  //   reader's three-key guard then saw all-falsy and skipped the assignment
  //   entirely, leaving conv.model undefined so the composer fell through to
  //   serverModel and painted the WRONG model (2026-07-27, conv
  //   ms352oniikgq10 — Opus 5 conv painted as the kimi-k3 default).
  //   Writer and reader must resolve identically or the cache silently
  //   downgrades a conversation's identity.
  function _extractSettings(conv) {
    return {
      model: conv.model || conv.preset || conv.effort,
      provider_id: conv.provider_id,
      thinkingDepth: conv.thinkingDepth,
      searchMode: conv.searchMode, fetchEnabled: conv.fetchEnabled,
      codeExecEnabled: conv.codeExecEnabled, browserEnabled: conv.browserEnabled,
      desktopEnabled: conv.desktopEnabled, memoryEnabled: conv.memoryEnabled,
      schedulerEnabled: conv.schedulerEnabled,
      autopilotEnabled: conv.autopilotEnabled,
      activeFlow: conv.activeFlow, imageGenMode: conv.imageGenMode,
      humanGuidanceEnabled: conv.humanGuidanceEnabled,
      planMode: conv.planMode,
      projectPath: conv.projectPath, projectPaths: conv.projectPaths,
      readOnlyPaths: conv.readOnlyPaths,
      autoTranslate: conv.autoTranslate,
      pinned: conv.pinned, pinnedAt: conv.pinnedAt,
      folderId: conv.folderId,
      /* Human-only autopilot run-summary sidecar — keep it in the cache so a
       * reload renders the run fold's report panel without a server round-trip. */
      autopilotSummaries: conv.autopilotSummaries,
    };
  }

  // ── Read API ──

  /**
   * Get locally observed conversation metadata.
   * @param {string} id
   * @returns {Promise<{id,title,updatedAt,cachedAt,settings,msgCount}|null>}
   */
  function getMeta(id) {
    if (!_available || !id) return Promise.resolve(null);
    return _open().then(function (db) {
      if (!db) return null;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(META_STORE, 'readonly');
          var req = tx.objectStore(META_STORE).get(id);
          req.onsuccess = function () { resolve(req.result || null); };
          req.onerror = function () {
            console.warn('[ConvCache] getMeta error:', req.error);
            resolve(null);
          };
        } catch (e) {
          console.warn('[ConvCache] getMeta exception:', e.message);
          resolve(null);
        }
      });
    });
  }

  /**
   * Replace the FULL-LIST sidebar mirror with the server's authoritative list.
   *
   * Distinct from `put()` (which writes one locally observed metadata row),
   * this stores a lightweight row per conversation the server reports, so boot
   * path can paint the ENTIRE sidebar before the network round-trip. Rewritten
   * wholesale (clear + bulk put) in ONE transaction so a conv deleted elsewhere
   * simply falls out of the mirror; the anti-resurrect judgement stays with the
   * caller (loadConversationCatalog's completeness check), never here.
   *
   * Only the cheap metadata is persisted (id/title/updatedAt/createdAt/rev/
   * msgCount/settings) — never message bodies.
   * @param {Array<object>} serverConvs rows from the ?meta=1 list response
   * @returns {Promise<number>} number of rows written
   */
  function putSidebarList(serverConvs) {
    if (!_available || !Array.isArray(serverConvs)) return Promise.resolve(0);
    return _open().then(function (db) {
      if (!db) return 0;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(SIDEBAR_STORE, 'readwrite');
          var store = tx.objectStore(SIDEBAR_STORE);
          store.clear();
          var written = 0;
          serverConvs.forEach(function (sc) {
            if (!sc || !sc.id) return;
            var count = sc.messageCount != null ? sc.messageCount
              : (sc.msgCount != null ? sc.msgCount : sc.msg_count);
            store.put({
              id: sc.id,
              title: sc.title || 'Untitled',
              createdAt: sc.createdAt || sc.created_at || 0,
              updatedAt: sc.updatedAt || sc.updated_at || sc.createdAt || 0,
              rev: (typeof sc.rev === 'number') ? sc.rev : null,
              msgCount: count || 0,
              settings: sc.settings || {},
            });
            written++;
          });
          tx.oncomplete = function () {
            console.debug('[ConvCache] putSidebarList wrote %d rows', written);
            resolve(written);
          };
          tx.onerror = function () {
            console.warn('[ConvCache] putSidebarList tx error: %o', tx.error);
            resolve(0);
          };
          tx.onabort = function () {
            console.warn('[ConvCache] putSidebarList tx aborted: %o', tx.error);
            resolve(0);
          };
        } catch (e) {
          console.warn('[ConvCache] putSidebarList exception: %s', e.message);
          resolve(0);
        }
      });
    });
  }

  /**
   * Read the FULL-LIST sidebar mirror written by `putSidebarList`. Used by the
   * boot path to paint the whole sidebar (not just opened convs) before the
   * server list arrives.
   * @returns {Promise<Array<{id,title,createdAt,updatedAt,rev,msgCount,settings}>>}
   */
  function getSidebarList() {
    if (!_available) return Promise.resolve([]);
    return _open().then(function (db) {
      if (!db) return [];
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(SIDEBAR_STORE, 'readonly');
          var out = [];
          var cursorReq = tx.objectStore(SIDEBAR_STORE).openCursor();
          cursorReq.onsuccess = function (e) {
            var c = e.target.result;
            if (c) { out.push(c.value); c.continue(); }
          };
          tx.oncomplete = function () { resolve(out); };
          tx.onerror = function () {
            console.warn('[ConvCache] getSidebarList tx error: %o', tx.error);
            resolve(out);
          };
        } catch (e) {
          console.warn('[ConvCache] getSidebarList exception: %s', e.message);
          resolve([]);
        }
      });
    });
  }

  /**
   * List ALL cached conversation meta rows (meta-only — no message join).
   * Cheap cursor over the conv_meta store; used by the boot path to paint
   * the sidebar instantly before / without a server round-trip.
   *
   * NOTE: `put()` writes rows encountered by local interactions, not the full
   * server list. `sidebar_meta` is the complete catalog mirror.
   * @returns {Promise<Array<{id,title,updatedAt,cachedAt,settings,msgCount}>>}
   */
  function getAllMeta() {
    if (!_available) return Promise.resolve([]);
    return _open().then(function (db) {
      if (!db) return [];
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(META_STORE, 'readonly');
          var out = [];
          var cursorReq = tx.objectStore(META_STORE).openCursor();
          cursorReq.onsuccess = function (e) {
            var c = e.target.result;
            if (c) {
              var v = c.value;
              out.push({
                id: v.id, title: v.title, updatedAt: v.updatedAt,
                cachedAt: v.cachedAt, settings: v.settings || {},
                // Pre-v4 metadata may only carry msgOrder. It is consulted once
                // for a count during rollout; the transcript itself is absent.
                msgCount: v.msgCount || (Array.isArray(v.msgOrder) ? v.msgOrder.length : 0),
              });
              c.continue();
            }
          };
          tx.oncomplete = function () { resolve(out); };
          tx.onerror = function () {
            console.warn('[ConvCache] getAllMeta tx error: %o', tx.error);
            resolve(out);
          };
        } catch (e) {
          console.warn('[ConvCache] getAllMeta exception: %s', e.message);
          resolve([]);
        }
      });
    });
  }

  // ── Write API ──

  /** Cache metadata/settings only; transcript ownership remains in TurnStore. */
  function put(conv) {
    if (!_available || !conv || !conv.id) return Promise.resolve();
    var convId = conv.id;
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(META_STORE, 'readwrite');
          tx.objectStore(META_STORE).put({
            id: convId,
            title: conv.title || 'Untitled',
            updatedAt: conv.updatedAt || conv.createdAt || Date.now(),
            cachedAt: Date.now(),
            settings: _extractSettings(conv),
            msgCount: Math.max(0, Number(conv._serverTurnCount) || 0),
          });
          tx.oncomplete = function () {
            _putsSinceEvict++;
            if (_putsSinceEvict >= EVICT_EVERY_N_PUTS) {
              _putsSinceEvict = 0;
              evict().then(function (n) {
                if (n > 0) console.info('[ConvCache] Periodic evict removed %d entries', n);
              });
              _maybeCheckQuota();
            }
            resolve();
          };
          tx.onerror = tx.onabort = function () {
            console.warn('[ConvCache] metadata put failed:', tx.error);
            resolve();
          };
        } catch (e) {
          console.warn('[ConvCache] put exception:', e.message);
          resolve();
        }
      });
    });
  }

  /** Remove a conversation from both metadata mirrors. */
  function remove(id) {
    if (!_available || !id) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, SIDEBAR_STORE], 'readwrite');
          tx.objectStore(META_STORE).delete(id);
          // Keep the full-list mirror consistent on an individual delete (a
          // wholesale putSidebarList would also drop it, but this reflects the
          // removal immediately so a boot before the next full load can't
          // repaint a just-deleted conv).
          tx.objectStore(SIDEBAR_STORE).delete(id);
          tx.oncomplete = function () {
            console.debug('[ConvCache] remove id=%s', id);
            resolve();
          };
          tx.onerror = function () {
            console.warn('[ConvCache] remove tx error id=%s: %o', id, tx.error);
            resolve();
          };
        } catch (e) {
          console.warn('[ConvCache] remove exception id=%s: %s', id, e.message);
          resolve();
        }
      });
    });
  }

  // ── Eviction ──

  /** Evict oldest opened-conversation metadata entries. */
  function evict() {
    if (!_available) return Promise.resolve(0);
    return _open().then(function (db) {
      if (!db) return 0;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(META_STORE, 'readwrite');
          var metaStore = tx.objectStore(META_STORE);
          var countReq = metaStore.count();
          countReq.onsuccess = function () {
            var total = countReq.result;
            if (total <= MAX_CACHED) { resolve(0); return; }
            var toDelete = total - MAX_CACHED;
            var deleted = 0;
            var idx = metaStore.index('cachedAt');
            var cursor = idx.openCursor(); // ascending = oldest first
            cursor.onsuccess = function (e) {
              var c = e.target.result;
              if (c && deleted < toDelete) {
                c.delete();
                deleted++;
                c.continue();
              } else {
                // Wait for tx.oncomplete so cascade deletes finish before we resolve.
              }
            };
            cursor.onerror = function () {
              console.warn('[ConvCache] evict cursor error after %d deletes: %o', deleted, cursor.error);
            };
            tx.oncomplete = function () { resolve(deleted); };
            tx.onerror = function () {
              console.warn('[ConvCache] evict tx error: %o', tx.error);
              resolve(deleted);
            };
          };
          countReq.onerror = function () {
            console.warn('[ConvCache] evict count error: %o', countReq.error);
            resolve(0);
          };
        } catch (e) {
          console.warn('[ConvCache] evict exception: %s', e.message);
          resolve(0);
        }
      });
    });
  }

  /** Clear all cached conversation metadata. */
  function clear() {
    if (!_available) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, SIDEBAR_STORE], 'readwrite');
          tx.objectStore(META_STORE).clear();
          tx.objectStore(SIDEBAR_STORE).clear();
          tx.oncomplete = function () {
            console.info('[ConvCache] ✅ Cache cleared');
            resolve();
          };
          tx.onerror = function () {
            console.warn('[ConvCache] clear tx error: %o', tx.error);
            resolve();
          };
        } catch (e) {
          console.warn('[ConvCache] clear exception: %s', e.message);
          resolve();
        }
      });
    });
  }

  /** Cache statistics for the metadata-only cache. */
  function stats() {
    if (!_available) return Promise.resolve({ count: 0, messageCount: 0, available: false });
    return _open().then(function (db) {
      if (!db) return { count: 0, messageCount: 0, available: false };
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(META_STORE, 'readonly');
          var metaCount = tx.objectStore(META_STORE).count();
          tx.oncomplete = function () {
            resolve({ count: metaCount.result, messageCount: 0, available: true });
          };
          tx.onerror = function () {
            console.warn('[ConvCache] stats tx error: %o', tx.error);
            resolve({ count: 0, messageCount: 0, available: true });
          };
        } catch (e) {
          console.warn('[ConvCache] stats exception: %s', e.message);
          resolve({ count: 0, messageCount: 0, available: false });
        }
      });
    });
  }

  function isAvailable() {
    return _available;
  }

  // ── Quota / persistent storage ──

  /**
   * Best-effort: ask the browser to mark our origin's storage as persistent
   * so it isn't evicted under disk pressure.  Browsers may decline silently.
   */
  function _requestPersistentStorage() {
    try {
      if (!navigator.storage || typeof navigator.storage.persist !== 'function') return;
      if (typeof navigator.storage.persisted === 'function') {
        navigator.storage.persisted().then(function (already) {
          if (already) {
            console.debug('[ConvCache] Storage already persistent');
            return;
          }
          navigator.storage.persist().then(function (granted) {
            console.info('[ConvCache] persist() granted=%s', granted);
          }, function (e) {
            console.warn('[ConvCache] persist() error: %s', e && e.message);
          });
        }, function () { /* ignore */ });
      } else {
        navigator.storage.persist().then(function (granted) {
          console.info('[ConvCache] persist() granted=%s', granted);
        }, function () { /* ignore */ });
      }
    } catch (e) {
      console.warn('[ConvCache] persist() unavailable: %s', e && e.message);
    }
  }

  /**
   * Throttled quota probe.  If we're within 50 MB of the cap, fire an
   * extra evict() so we don't hit QuotaExceededError mid-write.
   */
  function _maybeCheckQuota() {
    try {
      if (!navigator.storage || typeof navigator.storage.estimate !== 'function') return;
      var now = Date.now();
      if (now - _lastQuotaCheck < QUOTA_CHECK_INTERVAL_MS) return;
      _lastQuotaCheck = now;
      navigator.storage.estimate().then(function (est) {
        if (!est || !est.quota) return;
        var remaining = est.quota - (est.usage || 0);
        var bufferBytes = 50 * 1024 * 1024;
        var pct = est.quota > 0 ? ((est.usage || 0) / est.quota * 100).toFixed(1) : '?';
        console.debug('[ConvCache] quota usage=%s%% remaining=%d MB', pct, Math.round(remaining / 1024 / 1024));
        if (remaining < bufferBytes) {
          console.warn('[ConvCache] Approaching storage quota — triggering evict');
          evict().then(function (n) {
            if (n > 0) console.info('[ConvCache] Quota-pressure evict removed %d entries', n);
          });
        }
      }, function (e) {
        console.warn('[ConvCache] estimate() error: %s', e && e.message);
      });
    } catch (e) {
      console.warn('[ConvCache] quota check exception: %s', e && e.message);
    }
  }

  // ── Pre-warm: open DB on load so first cache hit is fast ──
  _open().then(function (db) {
    if (db) {
      console.info('[ConvCache] IndexedDB cache ready (db=%s v=%d)', DB_NAME, DB_VERSION);
      evict().then(function (n) {
        if (n > 0) console.info('[ConvCache] Evicted ' + n + ' old entries');
      });
      _requestPersistentStorage();
      _maybeCheckQuota();
    }
  });

  // ── Public API ──
  return {
    getMeta: getMeta,
    getAllMeta: getAllMeta,
    getSidebarList: getSidebarList,
    putSidebarList: putSidebarList,
    put: put,
    remove: remove,
    evict: evict,
    clear: clear,
    stats: stats,
    isAvailable: isAvailable,
    /* Test-only seam: the settings mirror is a pure function whose
     * resolution MUST stay identical to _applySettingsToConv's. Exposed so a
     * behaviour guard can round-trip it (writer → reader) without reaching
     * into the IIFE. Not used by product code. */
    __testExtractSettings: _extractSettings,
  };
})();
