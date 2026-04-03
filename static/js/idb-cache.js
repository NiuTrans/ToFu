// ══════════════════════════════════════════════════════
//  idb-cache.js — IndexedDB read-through cache for conversations
//
//  Architecture:
//    - Server (PostgreSQL) is the SINGLE SOURCE OF TRUTH
//    - IndexedDB is a LOCAL READ CACHE only — never authoritative
//    - On click: render from cache instantly, background-verify freshness
//    - On mutation: write-through to cache after server sync succeeds
//    - On failure: graceful fallback to server fetch (cache is optional)
//
//  Stores:
//    conversations — { id, title, messages, updatedAt, settings, cachedAt }
//
//  Eviction: LRU by cachedAt, max 200 conversations
// ══════════════════════════════════════════════════════

var ConvCache = (function () {
  'use strict';

  var DB_NAME = 'chatui_conv_cache';
  var DB_VERSION = 1;
  var STORE = 'conversations';
  var MAX_CACHED = 200;

  /** @type {IDBDatabase|null} */
  var _db = null;
  /** @type {Promise<IDBDatabase>|null} */
  var _dbPromise = null;
  var _available = true;  // false if IndexedDB is unavailable or errored

  // ── Open / Init ──

  function _open() {
    if (_dbPromise) return _dbPromise;
    if (!_available) return Promise.resolve(null);

    _dbPromise = new Promise(function (resolve, reject) {
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
          if (!db.objectStoreNames.contains(STORE)) {
            var store = db.createObjectStore(STORE, { keyPath: 'id' });
            store.createIndex('cachedAt', 'cachedAt', { unique: false });
          }
        };
        req.onsuccess = function (e) {
          _db = e.target.result;
          // Handle unexpected close (e.g. browser clearing storage)
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

  /**
   * Get a single conversation from cache.
   * @param {string} id
   * @returns {Promise<{id,title,messages,updatedAt,settings,cachedAt}|null>}
   */
  function get(id) {
    if (!_available) return Promise.resolve(null);
    return _open().then(function (db) {
      if (!db) return null;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readonly');
          var req = tx.objectStore(STORE).get(id);
          req.onsuccess = function () {
            var hit = req.result || null;
            if (hit) console.debug('[ConvCache] get hit id=%s msgs=%d cachedAt=%s', id, (hit.messages||[]).length, new Date(hit.cachedAt).toLocaleTimeString());
            resolve(hit);
          };
          req.onerror = function () {
            console.warn('[ConvCache] get error:', req.error);
            resolve(null);
          };
        } catch (e) {
          console.warn('[ConvCache] get exception:', e.message);
          resolve(null);
        }
      });
    });
  }

  /**
   * Store a conversation in cache.
   * Strips transient fields (_needsLoad, _hydratePromise, etc.) before storage.
   * @param {object} conv — the full conversation object
   * @returns {Promise<void>}
   */
  function put(conv) {
    if (!_available || !conv || !conv.id) return Promise.resolve();
    // Don't cache conversations with no messages (empty shells)
    if (!conv.messages || conv.messages.length === 0) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          // Strip transient/circular/large runtime-only fields
          var record = {
            id: conv.id,
            title: conv.title || 'Untitled',
            updatedAt: conv.updatedAt || conv.createdAt || Date.now(),
            cachedAt: Date.now(),
            // Lightweight settings snapshot (same fields as syncConversationToServer)
            settings: {
              model: conv.model, thinkingDepth: conv.thinkingDepth,
              searchMode: conv.searchMode, fetchEnabled: conv.fetchEnabled,
              codeExecEnabled: conv.codeExecEnabled, browserEnabled: conv.browserEnabled,
              desktopEnabled: conv.desktopEnabled, skillsEnabled: conv.skillsEnabled,
              schedulerEnabled: conv.schedulerEnabled, swarmEnabled: conv.swarmEnabled,
              endpointEnabled: conv.endpointEnabled, imageGenMode: conv.imageGenMode,
              humanGuidanceEnabled: conv.humanGuidanceEnabled,
              projectPath: conv.projectPath, projectPaths: conv.projectPaths,
              autoTranslate: conv.autoTranslate,
              pinned: conv.pinned, pinnedAt: conv.pinnedAt,
            },
            // Messages: strip base64 image data (re-hydrated on demand anyway)
            messages: conv.messages.map(function (m) {
              var r = {};
              // Copy all enumerable keys EXCEPT known bloat
              for (var k in m) {
                if (k === '_hydratePromise' || k === '_translateTaskId') continue;
                if (Object.prototype.hasOwnProperty.call(m, k)) r[k] = m[k];
              }
              // Strip base64 from images (they get re-hydrated from URL)
              if (r.images && r.images.length > 0) {
                r.images = r.images.map(function (img) {
                  var o = {};
                  for (var ik in img) {
                    if (ik === 'base64' || ik === 'preview') continue;
                    if (Object.prototype.hasOwnProperty.call(img, ik)) o[ik] = img[ik];
                  }
                  // Keep URL for re-hydration, and a short preview placeholder
                  if (img.url) { o.url = img.url; o.preview = img.url; }
                  return o;
                });
              }
              return r;
            }),
          };

          var tx = db.transaction(STORE, 'readwrite');
          tx.objectStore(STORE).put(record);
          tx.oncomplete = function () {
            console.debug('[ConvCache] put id=%s msgs=%d', record.id, record.messages.length);
            resolve();
          };
          tx.onerror = function () {
            console.warn('[ConvCache] put error:', tx.error);
            resolve();
          };
        } catch (e) {
          console.warn('[ConvCache] put exception:', e.message);
          resolve();
        }
      });
    });
  }

  /**
   * Remove a conversation from cache.
   * @param {string} id
   * @returns {Promise<void>}
   */
  function remove(id) {
    if (!_available || !id) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readwrite');
          tx.objectStore(STORE).delete(id);
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

  /**
   * Evict oldest entries to stay within MAX_CACHED.
   * Called periodically (not on every put — would be wasteful).
   * @returns {Promise<number>} number of entries evicted
   */
  function evict() {
    if (!_available) return Promise.resolve(0);
    return _open().then(function (db) {
      if (!db) return 0;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readwrite');
          var store = tx.objectStore(STORE);
          var countReq = store.count();
          countReq.onsuccess = function () {
            var total = countReq.result;
            if (total <= MAX_CACHED) { resolve(0); return; }
            var toDelete = total - MAX_CACHED;
            var deleted = 0;
            var idx = store.index('cachedAt');
            var cursor = idx.openCursor(); // ascending = oldest first
            cursor.onsuccess = function (e) {
              var c = e.target.result;
              if (c && deleted < toDelete) {
                c.delete();
                deleted++;
                c.continue();
              } else {
                resolve(deleted);
              }
            };
            cursor.onerror = function () {
              console.warn('[ConvCache] evict cursor error after %d deletes: %o', deleted, cursor.error);
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

  /**
   * Clear ALL cached conversations.
   * @returns {Promise<void>}
   */
  function clear() {
    if (!_available) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readwrite');
          tx.objectStore(STORE).clear();
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

  /**
   * Get cache statistics.
   * @returns {Promise<{count:number, available:boolean}>}
   */
  function stats() {
    if (!_available) return Promise.resolve({ count: 0, available: false });
    return _open().then(function (db) {
      if (!db) return { count: 0, available: false };
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, 'readonly');
          var req = tx.objectStore(STORE).count();
          req.onsuccess = function () {
            resolve({ count: req.result, available: true });
          };
          req.onerror = function () {
            console.warn('[ConvCache] stats count error: %o', req.error);
            resolve({ count: 0, available: true });
          };
        } catch (e) {
          console.warn('[ConvCache] stats exception: %s', e.message);
          resolve({ count: 0, available: false });
        }
      });
    });
  }

  /**
   * Check if cache is functional.
   * @returns {boolean}
   */
  function isAvailable() {
    return _available;
  }

  // ── Pre-warm: open DB on load so first cache hit is fast ──
  _open().then(function (db) {
    if (db) {
      console.info('[ConvCache] IndexedDB cache ready (db=' + DB_NAME + ')');
      // Background eviction on startup
      evict().then(function (n) {
        if (n > 0) console.info('[ConvCache] Evicted ' + n + ' old entries');
      });
    }
  });

  // ── Public API ──
  return {
    get: get,
    put: put,
    remove: remove,
    evict: evict,
    clear: clear,
    stats: stats,
    isAvailable: isAvailable,
  };
})();
