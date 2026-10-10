"""Compiled browser SSE framing, resource bounds, and disposal contracts."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sse_bundle(tmp_path):
    out = tmp_path / 'sse-reader.js'
    subprocess.run([
        'node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/core/sse-reader.ts'), '--bundle',
        '--format=iife', '--global-name=SSEReader', f'--outfile={out}',
    ], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
    return out


CHECKS = r'''async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const encode = text => new TextEncoder().encode(text);
  for (const mode of ['stop', 'callback-error', 'abort', 'oversize', 'multiline-oversize']) {
    let cancelled = 0;
    const abort = new AbortController();
    const response = new Response(new ReadableStream({
      start(controller) {
        if (mode === 'oversize') controller.enqueue(new Uint8Array(16 * 1024 * 1024 + 1).fill(120));
        else if (mode === 'multiline-oversize') {
          const line = encode(':' + 'x'.repeat(1022) + '\n');
          for (let i = 0; i < 16385; i++) controller.enqueue(line);
        } else if (mode !== 'abort') controller.enqueue(encode('data: hello\n\n'));
      },
      cancel() { cancelled++; return new Promise(() => {}); }
    }));
    let error;
    const result = SSEReader.readSSEStream(response, {
      signal: abort.signal,
      onLine() { if (mode === 'callback-error') throw new Error('callback broke'); return mode === 'stop'; }
    }).catch(e => { error = e; });
    if (mode === 'abort') abort.abort(new Error('cancelled by owner'));
    await result;
    check(!response.body.locked, mode + ' retained reader lock');
    check(cancelled === 1, mode + ' did not cancel exactly once');
    if (mode.includes('oversize')) check(error?.code === 'stream_resource_exhausted', mode + ' accepted unbounded data');
    if (mode === 'callback-error') check(error?.message === 'callback broke', 'masked callback error');
    if (mode === 'abort') check(error?.message === 'cancelled by owner', 'masked abort reason');
  }
  const bytes = encode('\uFEFFdata: 你好\r\n\rdata: end');
  const response = new Response(new ReadableStream({start(controller) {
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  }}));
  const lines = [];
  await SSEReader.readSSEStream(response, {onLine: line => { lines.push(line); }});
  check(JSON.stringify(lines) === JSON.stringify(['data: 你好', '', 'data: end']), 'split UTF8/CRLF/tail mismatch: ' + JSON.stringify(lines));
  check(!response.body.locked, 'EOF retained reader lock');
  return true;
}'''


@pytest.mark.unit
def test_sse_reader_faults_in_compiled_module(sse_bundle):
    script = sse_bundle.read_text() + '\n(' + CHECKS + ')().catch(e => {console.error(e);process.exitCode=1;});'
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.visual
def test_sse_reader_faults_in_real_browser(browser, live_server, sse_bundle):
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(live_server, wait_until='domcontentloaded')
        page.add_script_tag(content=sse_bundle.read_text())
        assert page.evaluate(CHECKS)
    finally:
        context.close()
