/* ===== migrated source: core/sse_reader.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/sse_reader.js — shared SSE fetch-response read/decode/buffer loop

   The mechanical core of consuming a streaming `fetch` Response — the
   `getReader()` + `TextDecoder` + `buffer.split("\n")` + `lines.pop()`
   tail-buffering loop — was copy-pasted verbatim across THREE call sites:

     • core/turn-state.ts   (native branch/turn stream)
     • paper-reader.js      (arXiv fetch stream)
     • ui/sse_pipeline.js   (main chat stream)

   Each copy carried the SAME transport bug-surface (partial-line reassembly
   across chunk boundaries, trailing-partial flush at stream end) while the
   per-line SEMANTICS differed per site. `readSSEStream` owns the transport
   loop only; every site passes its own `onLine` callback and (for the main
   chat) the per-chunk `onChunk` / `afterChunk` hooks. No behavior change:
   this is a pure consolidation of the loop, not the line handlers.

   It loads after core.js and shares window scope (no exports/imports); the
   loop touches only `response.body.getReader()` + `TextDecoder` (both
   platform globals), so it has no other cross-file dependency.

   Usage
   -----
       // Returns the boolean `onLine` signalled (true = done-early), or
       // false if the stream ended without `onLine` ever returning truthy.
       const done = await readSSEStream(response, {
         onLine(line) {            // one raw "\n"-delimited line (untrimmed)
           return processLine(line) === 'done';  // truthy → stop the loop
         },
         onChunk() { touchTimer(); },     // optional: per chunk, BEFORE decode
         afterChunk() { periodicSave(); },// optional: per chunk, AFTER lines
         flushTail: true,                 // default true: deliver the trailing
                                          //   partial (no final "\n") at end.
                                          //   Pass false to match a caller that
                                          //   never processed its tail.
       });

   Contract (locked by tests/test_frontend_sse_reader.py):
   - Complete lines are delivered to `onLine` in order.
   - A line split across two `read()` chunks is reassembled first.
   - `flushTail` true → the final partial is delivered once the stream closes.
   - `onLine` returning truthy stops the loop and resolves the promise `true`.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Drive the read/decode/buffer loop of an SSE `fetch` Response.
 *
 * @param {Response} response  A fetch Response with a readable `body`.
 * @param {object} opts
 * @param {(line:string)=>any} opts.onLine  Called with each raw line
 *   (NOT trimmed, NOT `data:`-stripped — the caller owns that). A truthy
 *   return stops the loop.
 * @param {()=>void} [opts.onChunk]     Called once per chunk BEFORE decode
 *   (e.g. keepalive-proves-alive timer touch).
 * @param {()=>void} [opts.afterChunk]  Called once per chunk AFTER its lines
 *   are processed (e.g. periodic save). Skipped for a chunk that stopped early.
 * @param {boolean} [opts.flushTail=true]  Deliver the trailing partial line
 *   (no terminating "\n") when the stream ends.
 * @returns {Promise<boolean>}  True iff `onLine` signalled done; else false.
 */
async function readSSEStream(response, opts) {
  const onLine = opts.onLine;
  const onChunk = opts.onChunk;
  const afterChunk = opts.afterChunk;
  const flushTail = opts.flushTail !== false;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      if (flushTail && buffer.trim()) {
        for (const line of buffer.split('\n')) {
          if (onLine(line)) return true;
        }
      }
      return false;
    }
    if (onChunk) onChunk();
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (onLine(line)) return true;
    }
    if (afterChunk) afterChunk();
  }
}

if (typeof window !== 'undefined') runtimeScope.readSSEStream = readSSEStream;
