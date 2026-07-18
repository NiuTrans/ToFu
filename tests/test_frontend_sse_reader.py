"""Contract tests for the shared SSE read/decode/buffer primitive
``static/js/core/sse_reader.js`` (``window.readSSEStream``).

WHY
---
The mechanical core of consuming an SSE fetch response — ``getReader()`` +
``TextDecoder`` + ``buffer.split("\\n")`` + ``lines.pop()`` tail-buffering —
was copy-pasted verbatim across THREE call sites:
  * ``static/js/branch.js`` (branch stream)
  * ``static/js/paper-reader.js`` (arXiv fetch stream)
  * ``static/js/ui/sse_pipeline.js`` (main chat stream)

``core/sse_reader.js`` extracts that loop as ``readSSEStream(response, opts)``
with callback seams (``onLine`` for per-line semantics, ``onChunk`` /
``afterChunk`` for the main-chat timer-touch + periodic-save hooks) so each
site keeps its own fragile per-line logic while sharing the transport loop.

These assertions lock the primitive's contract:
  1. Complete ``\\n``-delimited lines are delivered to ``onLine`` in order.
  2. A line split ACROSS two chunk boundaries is reassembled before delivery.
  3. The trailing partial (no final ``\\n``) is flushed at stream end when
     ``flushTail`` is true (branch + main-chat behavior), and NOT flushed when
     ``flushTail`` is false (paper's exact pre-extraction behavior).
  4. ``onLine`` returning a truthy value stops the loop early (the done signal).
  5. ``onChunk`` fires once per chunk BEFORE decode; ``afterChunk`` fires once
     per chunk AFTER the line loop (the two main-chat hooks).
  6. The call resolves to the done boolean.

Runs the REAL shipped JS under jsdom so it tracks the file. Skips cleanly when
node + jsdom aren't installed.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});

// A fake fetch Response whose body.getReader() yields the given byte chunks
// (strings, UTF-8 encoded) then {done:true}.
const enc = new TextEncoder();
function fakeResponse(chunks) {
  let i = 0;
  return {
    body: {
      getReader() {
        return {
          read() {
            if (i < chunks.length) {
              return Promise.resolve({ done: false, value: enc.encode(chunks[i++]) });
            }
            return Promise.resolve({ done: true, value: undefined });
          },
        };
      },
    },
  };
}

(async () => {
  check('readSSEStream is defined', typeof readSSEStream === 'function');
  if (typeof readSSEStream !== 'function') { report(); return; }

  // 1 + 2: split-across-chunks reassembly + in-order complete-line delivery.
  {
    const got = [];
    // "data: a" and "data: b" — the first line is split across two chunks.
    const done = await readSSEStream(
      fakeResponse(['data: a\ndata: ', 'b\ndata: c\n']),
      { onLine: (l) => { got.push(l); return false; } });
    check('delivers complete lines in order',
          got.join('|') === 'data: a|data: b|data: c');
    check('non-done stream resolves false', done === false);
  }

  // 3a: flushTail default true → trailing partial (no final \n) is delivered.
  {
    const got = [];
    await readSSEStream(fakeResponse(['data: a\ndata: tail']),
      { onLine: (l) => { got.push(l); return false; } });
    check('flushTail default delivers trailing partial',
          got.length === 2 && got[1] === 'data: tail');
  }

  // 3b: flushTail:false → trailing partial is NOT delivered (paper's behavior).
  {
    const got = [];
    await readSSEStream(fakeResponse(['data: a\ndata: tail']),
      { flushTail: false, onLine: (l) => { got.push(l); return false; } });
    check('flushTail:false drops trailing partial',
          got.length === 1 && got[0] === 'data: a');
  }

  // 4: onLine truthy return stops early — later lines are not delivered.
  {
    const got = [];
    const done = await readSSEStream(
      fakeResponse(['data: a\ndata: STOP\ndata: c\n']),
      { onLine: (l) => { got.push(l); return l === 'data: STOP'; } });
    check('truthy onLine stops the loop early',
          got.join('|') === 'data: a|data: STOP');
    check('early-stop resolves true', done === true);
  }

  // 5: onChunk fires per chunk BEFORE decode; afterChunk AFTER the line loop.
  {
    const order = [];
    await readSSEStream(fakeResponse(['data: a\n', 'data: b\n']), {
      onChunk: () => order.push('chunk'),
      onLine: (l) => { order.push('line'); return false; },
      afterChunk: () => order.push('after'),
    });
    // 2 chunks → chunk,line,after,chunk,line,after
    check('onChunk before line, afterChunk after line, per chunk',
          order.join(',') === 'chunk,line,after,chunk,line,after');
  }

  report();
})();
"""


def test_sse_reader_contract():
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'sse_reader.js'),
        body_js=_BODY,
        min_pass=8,
    )
