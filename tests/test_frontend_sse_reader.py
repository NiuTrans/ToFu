"""Typed SSE transport reader behavior and production ownership contracts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/core/sse-reader.ts'
OWNER_BUNDLE = native_module_path('.native/sse-reader-contract.js', OWNER)


def _run_node(script: str) -> str:
    process = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True,
        timeout=60,
    )
    output = (process.stdout or '') + (process.stderr or '')
    assert process.returncode == 0, output
    return output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_sse_reader_public_behavior():
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
const encoder = new TextEncoder();

function responseFromBytes(chunks) {
  let index = 0;
  return {
    body: {
      getReader() {
        return {
          read() {
            if (index < chunks.length) {
              return Promise.resolve({ done: false, value: chunks[index++] });
            }
            return Promise.resolve({ done: true, value: undefined });
          },
        };
      },
    },
  };
}

function responseFromText(chunks) {
  return responseFromBytes(chunks.map(chunk => encoder.encode(chunk)));
}

(async () => {
  check('reader_is_public', typeof readSSEStream === 'function');

  {
    const lines = [];
    const done = await readSSEStream(
      responseFromText(['data: a\ndata: ', 'b\ndata: c\n']),
      { onLine: line => { lines.push(line); return false; } },
    );
    check('split_lines_are_reassembled_in_order',
      lines.join('|') === 'data: a|data: b|data: c');
    check('eof_without_signal_returns_false', done === false);
  }

  {
    const lines = [];
    await readSSEStream(responseFromText(['data: a\ndata: tail']), {
      onLine: line => { lines.push(line); return false; },
    });
    check('tail_is_flushed_by_default',
      lines.length === 2 && lines[1] === 'data: tail');
  }

  {
    const lines = [];
    await readSSEStream(responseFromText(['data: a\ndata: tail']), {
      flushTail: false,
      onLine: line => { lines.push(line); return false; },
    });
    check('disabled_tail_flush_drops_partial_line',
      lines.length === 1 && lines[0] === 'data: a');
  }

  {
    const lines = [];
    const done = await readSSEStream(
      responseFromText(['data: a\ndata: STOP\ndata: c\n']),
      { onLine: line => { lines.push(line); return line === 'data: STOP'; } },
    );
    check('truthy_line_handler_stops_before_later_lines',
      lines.join('|') === 'data: a|data: STOP');
    check('early_stop_returns_true', done === true);
  }

  {
    const order = [];
    await readSSEStream(responseFromText(['data: a\n', 'data: b\n']), {
      onChunk: () => order.push('chunk'),
      onLine: () => { order.push('line'); return false; },
      afterChunk: () => order.push('after'),
    });
    check('chunk_hooks_keep_their_order',
      order.join(',') === 'chunk,line,after,chunk,line,after');
  }

  {
    const bytes = encoder.encode('data: 雪\n');
    const lines = [];
    await readSSEStream(
      responseFromBytes([bytes.slice(0, 7), bytes.slice(7, 8), bytes.slice(8)]),
      { onLine: line => { lines.push(line); return false; } },
    );
    check('utf8_code_points_survive_byte_boundaries', lines[0] === 'data: 雪');
  }

  {
    const order = [];
    await readSSEStream(responseFromText(['data: STOP\n']), {
      onChunk: () => order.push('chunk'),
      onLine: () => { order.push('line'); return true; },
      afterChunk: () => order.push('after'),
    });
    check('early_stop_skips_after_chunk', order.join(',') === 'chunk,line');
  }

  console.log(checks.join('\n'));
  if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch(error => { console.error(error); process.exit(1); });
'''.replace('OWNER_PATH', json.dumps(OWNER_BUNDLE))
    output = _run_node(script)
    assert output.count('PASS') == 10, output
