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
//  Schema v2 (split-store, diff writes):
//    conv_meta — keyed by `id`, holds metadata + ordered list of message
//                ids (with content hashes for diff detection).
//                Indexed by `cachedAt` for LRU eviction.
//    messages  — keyed by `[convId, msgId]`, one row per message.
//                Cascade-delete via composite-key range queries.
//
//  Diff writes: on `put(conv)`, we compare each message's stable
//  `_msgId` + content hash against the cached msgOrder.  Only added,
//  hash-changed, or removed messages touch the messages store; the
//  meta row is always rewritten (cheap).  This makes a regenerate /
//  edit / branch-truncate cost O(touched) instead of O(all).
//
//  Eviction: LRU by cachedAt on conv_meta, max 200 conversations.
//  Eviction cascades to the messages store via composite-key range delete.
// ══════════════════════════════════════════════════════

var ConvCache = (function () {
  'use strict';

  var DB_NAME = 'tofu_conv_cache';
  var LEGACY_DB_NAME = 'chatui_conv_cache';
  var DB_VERSION = 2;
  var META_STORE = 'conv_meta';
  var MSG_STORE = 'messages';
  var LEGACY_V1_STORE = 'conversations';
  var MAX_CACHED = 200;
  // Trigger eviction every N puts so long-lived tabs don't grow past MAX_CACHED.
  var EVICT_EVERY_N_PUTS = 20;
  // Quota check cadence — once per session is plenty; estimate() is non-trivial.
  var QUOTA_CHECK_INTERVAL_MS = 5 * 60 * 1000;
  // Range-key sentinels for "all messages of this conv".  Composite keys
  // are compared lexicographically; '' is min, '\uffff' is a safe max for
  // any UUID we'd ever generate.
  var KEY_MIN = '';
  var KEY_MAX = '\uffff';

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
          if (!db.objectStoreNames.contains(MSG_STORE)) {
            // Composite key [convId, msgId] — cascade-delete via range queries.
            db.createObjectStore(MSG_STORE, { keyPath: ['convId', 'msgId'] });
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

  // FNV-1a 32-bit hash of a string.  Cheap, deterministic, collision rate
  // acceptable for diff detection (worst case is one extra rewrite).
  function _hash32(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h.toString(36);
  }

  // Strip a single message of transient/bloat fields and return the
  // shape we want to persist.  Mirrors the v1 stripping logic.
  function _stripMessage(m) {
    var r = {};
    for (var k in m) {
      if (k === '_hydratePromise' || k === '_translateTaskId') continue;
      if (Object.prototype.hasOwnProperty.call(m, k)) r[k] = m[k];
    }
    if (r.images && r.images.length > 0) {
      r.images = r.images.map(function (img) {
        var o = {};
        for (var ik in img) {
          if (ik === 'base64' || ik === 'preview') continue;
          if (Object.prototype.hasOwnProperty.call(img, ik)) o[ik] = img[ik];
        }
        if (img.url) { o.url = img.url; o.preview = img.url; }
        return o;
      });
    }
    return r;
  }

  // Resolve a stable cache key for a message.  Prefers the bilateral
  // `_msgId` convention (server uuid or client `tmp_<uuid>`).  Falls
  // back to a positional key only when the message hasn't been stamped
  // — defensive; current stamping discipline makes that unreachable in
  // practice (see .tofu/skills/bilateral-msgid-and-data-msg-id.md).
  function _msgKey(msg, idx) {
    if (msg && typeof msg._msgId === 'string' && msg._msgId) return msg._msgId;
    return '_idx_' + idx;
  }

  // Settings whitelist — same fields the v1 record persisted.
  function _extractSettings(conv) {
    return {
      model: conv.model, thinkingDepth: conv.thinkingDepth,
      searchMode: conv.searchMode, fetchEnabled: conv.fetchEnabled,
      codeExecEnabled: conv.codeExecEnabled, browserEnabled: conv.browserEnabled,
      desktopEnabled: conv.desktopEnabled, memoryEnabled: conv.memoryEnabled,
      schedulerEnabled: conv.schedulerEnabled, swarmEnabled: conv.swarmEnabled,
      endpointEnabled: conv.endpointEnabled, imageGenMode: conv.imageGenMode,
      humanGuidanceEnabled: conv.humanGuidanceEnabled,
      projectPath: conv.projectPath, projectPaths: conv.projectPaths,
      autoTranslate: conv.autoTranslate,
      pinned: conv.pinned, pinnedAt: conv.pinnedAt,
      folderId: conv.folderId,
    };
  }

  // ── Read API ──

  /**
   * Get conversation metadata only (cheap — no message join).
   * @param {string} id
   * @returns {Promise<{id,title,updatedAt,cachedAt,settings,msgOrder,msgCount}|null>}
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
   * Paginated read.
   * @param {string} convId
   * @param {{beforeIdx?:number, afterIdx?:number, limit?:number}} opts
   *   beforeIdx — return messages with idx in [..., beforeIdx)
   *   afterIdx  — return messages with idx in (afterIdx, ...]
   *   limit     — max number to return (no limit if omitted).
   * @returns {Promise<Array<object>>} messages in conv order
   */
  function getMessages(convId, opts) {
    opts = opts || {};
    if (!_available || !convId) return Promise.resolve([]);
    return _open().then(function (db) {
      if (!db) return [];
      return getMeta(convId).then(function (meta) {
        if (!meta || !meta.msgOrder || meta.msgOrder.length === 0) return [];
        // Window the msgOrder array based on the requested range.
        var order = meta.msgOrder;
        var start = 0;
        var end = order.length;
        if (typeof opts.afterIdx === 'number') start = Math.max(0, opts.afterIdx + 1);
        if (typeof opts.beforeIdx === 'number') end = Math.min(end, opts.beforeIdx);
        if (typeof opts.limit === 'number' && opts.limit >= 0) {
          // For "last N" reads (typical chat history paging from the
          // bottom), use beforeIdx + limit to take the most-recent
          // `limit` from the windowed range.
          if (typeof opts.beforeIdx === 'number' || typeof opts.afterIdx !== 'number') {
            start = Math.max(start, end - opts.limit);
          } else {
            end = Math.min(end, start + opts.limit);
          }
        }
        var slice = order.slice(start, end);
        if (slice.length === 0) return [];
        return new Promise(function (resolve) {
          try {
            var tx = db.transaction(MSG_STORE, 'readonly');
            var store = tx.objectStore(MSG_STORE);
            var out = new Array(slice.length);
            var pending = slice.length;
            var failed = false;
            slice.forEach(function (entry, i) {
              var req = store.get([convId, entry.id]);
              req.onsuccess = function () {
                if (req.result && req.result.data) out[i] = req.result.data;
                if (--pending === 0 && !failed) {
                  resolve(out.filter(function (m) { return !!m; }));
                }
              };
              req.onerror = function () {
                if (failed) return;
                failed = true;
                console.warn('[ConvCache] getMessages get error: %o', req.error);
                resolve([]);
              };
            });
          } catch (e) {
            console.warn('[ConvCache] getMessages exception:', e.message);
            resolve([]);
          }
        });
      });
    });
  }

  /**
   * Compat: return the joined `{ ...meta, messages: [...] }` shape.
   * Used by the existing 30+ callers that expect the v1 surface.
   * Internally does a meta lookup + a bulk message fetch in one tx.
   * @param {string} id
   */
  function get(id) {
    if (!_available || !id) return Promise.resolve(null);
    return getMeta(id).then(function (meta) {
      if (!meta) return null;
      return getMessages(id).then(function (messages) {
        var out = {
          id: meta.id,
          title: meta.title,
          updatedAt: meta.updatedAt,
          cachedAt: meta.cachedAt,
          messages: messages,
        };
        // Spread settings back to the conv-level shape v1 returned.
        if (meta.settings) {
          for (var k in meta.settings) {
            if (Object.prototype.hasOwnProperty.call(meta.settings, k)) {
              out[k] = meta.settings[k];
            }
          }
        }
        // Also expose a nested `settings` for any caller that read it.
        out.settings = meta.settings || {};
        console.debug('[ConvCache] get hit id=%s msgs=%d cachedAt=%s', id, messages.length, new Date(meta.cachedAt).toLocaleTimeString());
        return out;
      });
    });
  }

  // ── Write API ──

  /**
   * Diff-write a conversation to cache.  Compares each message's
   * `_msgId` + content hash against the cached msgOrder; only changed
   * rows touch the messages store.  Always rewrites the meta row.
   * @param {object} conv
   * @returns {Promise<void>}
   */
  function put(conv) {
    if (!_available || !conv || !conv.id) return Promise.resolve();
    if (!conv.messages || conv.messages.length === 0) return Promise.resolve();
    var convId = conv.id;
    return _open().then(function (db) {
      if (!db) return;

      // Build the new msgOrder + a map id → stripped message.
      var newOrder = [];
      var newById = Object.create(null);
      conv.messages.forEach(function (m, i) {
        var stripped = _stripMessage(m);
        var id = _msgKey(m, i);
        var serialized = JSON.stringify(stripped);
        var h = _hash32(serialized);
        // De-duplicate ids inside a single conv by suffixing — should
        // never happen in practice but defends against bugs in callers.
        if (newById[id]) {
          var suffix = 1;
          while (newById[id + '#' + suffix]) suffix++;
          id = id + '#' + suffix;
        }
        newOrder.push({ id: id, h: h });
        newById[id] = stripped;
      });

      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, MSG_STORE], 'readwrite');
          var metaStore = tx.objectStore(META_STORE);
          var msgStore = tx.objectStore(MSG_STORE);

          // Read current meta to compute the diff.
          var metaReq = metaStore.get(convId);
          metaReq.onsuccess = function () {
            var oldMeta = metaReq.result;
            var oldOrder = (oldMeta && oldMeta.msgOrder) || [];
            var oldHashById = Object.create(null);
            oldOrder.forEach(function (e) { oldHashById[e.id] = e.h; });
            var newIdSet = Object.create(null);
            newOrder.forEach(function (e) { newIdSet[e.id] = true; });

            var added = 0, changed = 0, deleted = 0;

            // Delete ids present in old but not in new.
            oldOrder.forEach(function (e) {
              if (!newIdSet[e.id]) {
                msgStore.delete([convId, e.id]);
                deleted++;
              }
            });

            // Add or update ids whose hash differs (or is new).
            newOrder.forEach(function (e) {
              var oldH = oldHashById[e.id];
              if (oldH === undefined) {
                msgStore.put({ convId: convId, msgId: e.id, data: newById[e.id] });
                added++;
              } else if (oldH !== e.h) {
                msgStore.put({ convId: convId, msgId: e.id, data: newById[e.id] });
                changed++;
              }
            });

            // Always rewrite meta — cheap, and bumps cachedAt for LRU.
            var metaRecord = {
              id: convId,
              title: conv.title || 'Untitled',
              updatedAt: conv.updatedAt || conv.createdAt || Date.now(),
              cachedAt: Date.now(),
              settings: _extractSettings(conv),
              msgOrder: newOrder,
              msgCount: newOrder.length,
            };
            metaStore.put(metaRecord);

            tx.oncomplete = function () {
              if (added || changed || deleted) {
                console.debug(
                  '[ConvCache] put id=%s msgs=%d (+%d ~%d -%d)',
                  convId, newOrder.length, added, changed, deleted
                );
              } else {
                console.debug('[ConvCache] put id=%s msgs=%d (meta-only)', convId, newOrder.length);
              }
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
            tx.onerror = function () {
              console.warn('[ConvCache] put tx error:', tx.error);
              resolve();
            };
          };
          metaReq.onerror = function () {
            console.warn('[ConvCache] put meta-read error:', metaReq.error);
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
   * Remove a conversation: delete meta row + cascade-delete all
   * messages for that conv via composite-key range.
   * @param {string} id
   */
  function remove(id) {
    if (!_available || !id) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, MSG_STORE], 'readwrite');
          tx.objectStore(META_STORE).delete(id);
          var range = IDBKeyRange.bound([id, KEY_MIN], [id, KEY_MAX]);
          tx.objectStore(MSG_STORE).delete(range);
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

  /**
   * Evict oldest entries to stay within MAX_CACHED.  Cascades message
   * deletion in the same transaction.
   * @returns {Promise<number>} number of meta rows evicted
   */
  function evict() {
    if (!_available) return Promise.resolve(0);
    return _open().then(function (db) {
      if (!db) return 0;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, MSG_STORE], 'readwrite');
          var metaStore = tx.objectStore(META_STORE);
          var msgStore = tx.objectStore(MSG_STORE);
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
                var convId = c.value && c.value.id;
                c.delete();
                if (convId) {
                  var range = IDBKeyRange.bound([convId, KEY_MIN], [convId, KEY_MAX]);
                  msgStore.delete(range);
                }
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

  /**
   * Clear ALL cached conversations (both stores).
   */
  function clear() {
    if (!_available) return Promise.resolve();
    return _open().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, MSG_STORE], 'readwrite');
          tx.objectStore(META_STORE).clear();
          tx.objectStore(MSG_STORE).clear();
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
   * Cache statistics: number of cached conversations + total messages.
   */
  function stats() {
    if (!_available) return Promise.resolve({ count: 0, messageCount: 0, available: false });
    return _open().then(function (db) {
      if (!db) return { count: 0, messageCount: 0, available: false };
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction([META_STORE, MSG_STORE], 'readonly');
          var metaCount = tx.objectStore(META_STORE).count();
          var msgCount = tx.objectStore(MSG_STORE).count();
          tx.oncomplete = function () {
            resolve({ count: metaCount.result, messageCount: msgCount.result, available: true });
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
    get: get,
    getMeta: getMeta,
    getMessages: getMessages,
    put: put,
    remove: remove,
    evict: evict,
    clear: clear,
    stats: stats,
    isAvailable: isAvailable,
  };
})();
