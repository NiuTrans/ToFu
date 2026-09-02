/* ===== migrated source: core/async_pool.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/async_pool.js — bounded-concurrency task runner.

   ONE small primitive shared by every "sweep over N conversations on
   wake/reconnect" path. Without a cap, a tab that slept overnight with
   500+ conversations fires all N fetch/SSE-reattach calls at the same
   instant when it wakes (`visibilitychange` / `online` / boot-reconnect).
   That reconnect "thundering herd" saturates the event loop and — over a
   constrained proxy — the network, which is the front-end half of the
   observed overnight slow-recovery (the back-end half was the inline
   bundle rebuild, fixed in bf7c9b9).

   runWithConcurrency(items, worker, limit) runs `worker(item, index)` over
   `items` with at most `limit` in flight at once, draining as each settles.
   A rejected worker never aborts the pool (mirrors the existing
   Promise.all(...map) call-sites which already swallow per-item errors);
   errors are collected and returned so a caller can inspect them.

   Leaf module — references only `window` at load; called at runtime by
   core/conversation_invalidation.js and core/health_stream_timer.js. Bundled by
   Vite's module graph (the migrated module graph) BEFORE those consumers.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Run `worker` over `items` with a ceiling of `limit` concurrent calls.
 *
 * @param {Array} items         The work items.
 * @param {(item:any, index:number)=>any} worker  Async (or sync) per-item fn.
 * @param {number} [limit=4]     Max in-flight workers. Clamped to >=1.
 * @returns {Promise<{completed:number, errors:Array}>} resolves when every
 *          item has settled. Never rejects — a worker throw is captured.
 */
function runWithConcurrency(items, worker, limit) {
  var list = Array.isArray(items) ? items : [];
  var cap = (typeof limit === 'number' && limit >= 1) ? Math.floor(limit) : 4;
  return new Promise(function (resolve) {
    if (list.length === 0) { resolve({ completed: 0, errors: [] }); return; }
    var next = 0;       // index of the next item to dispatch
    var active = 0;     // workers currently in flight
    var completed = 0;
    var errors = [];
    var settled = false;

    function pump() {
      if (settled) return;
      if (completed >= list.length) {
        settled = true;
        resolve({ completed: completed, errors: errors });
        return;
      }
      while (active < cap && next < list.length) {
        /* Block-scoped per iteration: the worker runs in a later microtask, so
         *   a function-scoped `var` here would be overwritten by subsequent
         *   loop iterations before the deferred worker reads it — every
         *   dispatched worker would then see the LAST item (items 0..cap-2
         *   silently dropped). `let` gives each iteration its own binding. */
        let idx = next++;
        let item = list[idx];
        active++;
        Promise.resolve()
          .then(function () { return worker(item, idx); })
          .catch(function (e) { errors.push(e); })
          .then(function () {
            active--;
            completed++;
            pump();
          });
      }
    }
    pump();
  });
}

if (typeof window !== 'undefined') runtimeScope.runWithConcurrency = runWithConcurrency;
