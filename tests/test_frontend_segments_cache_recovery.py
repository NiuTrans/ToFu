"""Frontend segment-cache recovery — double-neuter tests (epic pt_cb8f98b0cb9b47fb).

Symptom the fix addresses: opening a conversation shows only the final
assistant deliverable — the tool-role + thinking timeline is missing — but a
hard refresh / conversation toggle brings it back. Root cause is a
segment-less IndexedDB cache that the Phase-2 freshness check refuses to
replace with the segment-carrying server copy (the GET-path rehydrate is
display-only: it does NOT bump count/updatedAt).

Two independent halves, each with a biting negative control:

  1. SOURCE — static/js/ui/sse_pipeline.js: the terminal `done` handler's
     `committedMessage` projection copies `segments` onto the settled in-memory
     assistant message (verbatim, like toolRounds). Without it the finalized
     message — and the ConvCache.put(conv) at finishStream — is seeded
     segment-less. NC: strip the projection line → segments do NOT land.

  2. RECOVERY — static/js/core/conversations.js: `_serverHasSegmentsLocalLacks`
     makes "the server GET carries segments the cached copy lacks" an explicit
     staleness signal so `cacheIsStale` flips true and `conv.messages` is
     replaced with the rehydrated server copy (→ re-render with tools/thinking)
     even when count + updatedAt match. NC: drop the predicate from the OR →
     the segment-less cache is judged FRESH and the server copy discarded.

Runs the REAL shipped source under node (no bundler, no DOM). The predicate and
the projection are pure, so no jsdom is needed. Skips cleanly when node is
absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'frontend', 'src', 'runtime')
# Located by SYMBOL: this predicate was extracted out of core/conversations.js
# in pt_3879f00e slice 3, which is what broke the hard-coded-path version.
CONV_JS = sources_defining('_serverHasSegmentsLocalLacks')[0]
SSE_JS = os.path.join(JS_DIR, 'app-runtime.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_plain_fn(src: str, name: str) -> str:
    """Extract a top-level `function <name>(...) { ... }` by brace matching."""
    m = re.search(r'(async\s+)?function %s\s*\(' % re.escape(name), src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


def _run_node(script: str) -> dict:
    out = subprocess.run(
        ['node', '-e', script], capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert out.returncode == 0, f'node failed: {out.stderr}\n---\n{out.stdout}'
    last = [ln for ln in out.stdout.strip().splitlines()
            if ln.strip().startswith('{')][-1]
    return json.loads(last)


# ─────────────────────────────────────────────────────────────────────────
#  Part 1 — RECOVERY predicate: `_serverHasSegmentsLocalLacks` +
#           the `cacheIsStale` decision it feeds.
# ─────────────────────────────────────────────────────────────────────────

_STALE_DECISION = r"""
function cacheIsStale(cacheHit, serverMsgs, localMsgs, serverUpdatedAt, cachedUpdatedAt, USE_SEG) {
  return !cacheHit ||
    serverMsgs.length !== localMsgs.length ||
    serverUpdatedAt > (cachedUpdatedAt || 0) ||
    (USE_SEG ? _serverHasSegmentsLocalLacks(serverMsgs, localMsgs) : false);
}
"""

_PART1_HARNESS = r"""
'use strict';
const window = {};
__PREDICATE__
__DECISION__

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A segment-CARRYING server copy vs a segment-LESS cache, SAME count + SAME
// updatedAt (the exact display-only-rehydrate situation).
const server = [
  { role: 'user', content: 'U1', _msgId: 'm0' },
  { role: 'assistant', content: 'A1', _msgId: 'm1',
    toolRounds: [{ roundNum: 1, status: 'done' }],
    thinking: 'reasoned',
    segments: [
      { type: 'thinking', text: 'reasoned', llmRound: 1 },
      { type: 'tool_use', id: 't1', name: 'read_files', llmRound: 1 },
      { type: 'text', text: 'A1', deliverable: true, terminal: true, llmRound: 1 },
    ] },
];
const cache = [
  { role: 'user', content: 'U1', _msgId: 'm0' },
  { role: 'assistant', content: 'A1', _msgId: 'm1',
    toolRounds: [{ roundNum: 1, status: 'done' }],
    thinking: 'reasoned' },  // ← NO segments (segment-less cache)
];

check('predicate_detects_missing_segments',
  _serverHasSegmentsLocalLacks(server, cache) === true);
const cacheWithSegs = JSON.parse(JSON.stringify(cache));
cacheWithSegs[1].segments = server[1].segments;
check('predicate_false_when_local_has_segments',
  _serverHasSegmentsLocalLacks(server, cacheWithSegs) === false);
check('predicate_ignores_non_assistant',
  _serverHasSegmentsLocalLacks(
    [{ role: 'user', segments: [{type:'text'}] }],
    [{ role: 'user' }]) === false);

// THE FIX: same count + same updatedAt, cacheHit=true → WITH the predicate the
// decision is STALE (server copy adopted → re-render with tools/thinking).
check('decision_stale_with_predicate',
  cacheIsStale(true, server, cache, 1000, 1000, true) === true);
// NC (biting): drop the segment clause → the SAME inputs are judged FRESH.
check('NC_decision_fresh_without_predicate',
  cacheIsStale(true, server, cache, 1000, 1000, false) === false);
// Sanity: a real count/updatedAt change is still stale regardless (no regression).
check('count_change_still_stale',
  cacheIsStale(true, server.concat([{role:'user'}]), cache, 1000, 1000, false) === true);

console.log(JSON.stringify({ out }));
"""


def _run_part1(use_real_predicate: bool = True) -> dict:
    src = open(CONV_JS, encoding='utf-8').read()
    if use_real_predicate:
        predicate = _extract_plain_fn(src, '_serverHasSegmentsLocalLacks')
    else:
        predicate = 'function _serverHasSegmentsLocalLacks() { return false; }'
    script = (_PART1_HARNESS
              .replace('__PREDICATE__', predicate)
              .replace('__DECISION__', _STALE_DECISION))
    return _run_node(script)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_recovery_predicate_flips_cacheisstale():
    """REAL `_serverHasSegmentsLocalLacks`: segment-less cache + segment server
    copy at equal count/updatedAt → cacheIsStale true (server copy wins)."""
    r = _run_part1(use_real_predicate=True)
    fails = [ln for ln in r['out'] if ln.startswith('FAIL')]
    assert not fails, 'recovery predicate failures:\n' + '\n'.join(r['out'])
    assert 'PASS decision_stale_with_predicate' in r['out']
    assert 'PASS NC_decision_fresh_without_predicate' in r['out']


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_without_predicate_cache_stays_fresh():
    """NC: with the segment clause removed, the segment-less cache is judged
    FRESH → the server's rehydrated segments would be discarded (the bug)."""
    r = _run_part1(use_real_predicate=False)
    assert 'FAIL predicate_detects_missing_segments' in r['out'], (
        'NC did not bite — a null predicate should fail detection:\n'
        + '\n'.join(r['out']))
    assert 'FAIL decision_stale_with_predicate' in r['out'], (
        'NC did not bite — without the predicate the decision is not stale:\n'
        + '\n'.join(r['out']))


# ─────────────────────────────────────────────────────────────────────────
#  Part 2 — SOURCE projection: the REAL committedMessage block copies
#           `segments` onto the settled assistant message.
# ─────────────────────────────────────────────────────────────────────────

def _extract_committed_block(src: str) -> str:
    """Slice the committedMessage projection body from sse_pipeline.js.

    From `const _cm = ev.committedMessage;` up to and including the closing
    brace of the `if (_cm && ...) { ... }` guard (the line after
    `assistantMsg._committedProjection = true;`) — the self-contained
    projection we can eval against a stub assistantMsg + ev. The end marker
    must include that closing brace or the sliced snippet is unbalanced (the
    guard opens a `{` right after the start line)."""
    start = src.index('const _cm = ev.committedMessage;')
    anchor = 'assistantMsg._committedProjection = true;'
    a = src.index(anchor, start) + len(anchor)
    # Consume through the next `}` (the guard's closing brace).
    end = src.index('}', a) + 1
    return src[start:end]


_PART2_HARNESS = r"""
'use strict';
// Stubs the projection block may close over (varies by source revision).
function _snapshotLongerRounds(_a, b) { return b; }
function _stampSegTranslations() { /* translate-race apply — not under test */ }

const ev = {
  committedMessage: {
    role: 'assistant',
    content: 'final answer',
    thinking: 'my reasoning',
    toolRounds: [{ roundNum: 1, status: 'done', toolName: 'read_files' }],
    finishReason: 'stop',
    usage: { output_tokens: 5 },
    segments: [
      { type: 'thinking', text: 'my reasoning', llmRound: 1 },
      { type: 'tool_use', id: 't1', name: 'read_files', llmRound: 1 },
      { type: 'text', text: 'final answer', deliverable: true, terminal: true, llmRound: 1 },
    ],
  },
};
// A fresh in-memory assistant message as it exists at `done`: has toolRounds +
// thinking from the stream, but NO segments (the pre-fix seed state).
const assistantMsg = { role: 'assistant', content: '', thinking: '', toolRounds: [] };

__BLOCK__

console.log(JSON.stringify({
  hasSegments: Array.isArray(assistantMsg.segments),
  segCount: Array.isArray(assistantMsg.segments) ? assistantMsg.segments.length : 0,
  toolUseId: (assistantMsg.segments || []).filter(s => s.type === 'tool_use').map(s => s.id),
  content: assistantMsg.content,
  projected: assistantMsg._committedProjection === true,
}));
"""


def _run_part2(strip_segments_line: bool = False) -> dict:
    src = open(SSE_JS, encoding='utf-8').read()
    block = _extract_committed_block(src)
    if strip_segments_line:
        # NC: remove the exact projection assignment the fix relies on. Matches
        # both the guarded one-liner and the multi-line `{ assistantMsg.segments
        # = _cm.segments; ... }` form — replace only the assignment statement.
        neutered = re.sub(
            r'assistantMsg\.segments\s*=\s*_cm\.segments;',
            '/* segments projection NEUTERED */;',
            block, count=1)
        assert neutered != block, 'NC did not strip the segments projection line'
        block = neutered
    script = _PART2_HARNESS.replace('__BLOCK__', block)
    return _run_node(script)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_committed_projection_copies_segments():
    """REAL committedMessage block projects `segments` verbatim onto the
    settled assistant message (so ConvCache.put seeds a segment-CARRYING cache)."""
    r = _run_part2(strip_segments_line=False)
    assert r['hasSegments'] and r['segCount'] == 3, r
    assert r['toolUseId'] == ['t1'], r
    assert r['projected'] is True, r
    assert r['content'] == 'final answer', r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_stripped_projection_drops_segments():
    """NC: remove the segments projection assignment → the settled message has
    NO segments (proving the projection is load-bearing, not incidental)."""
    r = _run_part2(strip_segments_line=True)
    assert not r['hasSegments'], (
        'NC did not bite — segments still present after stripping the '
        'projection line: %r' % r)
    # Everything else still projects (the neuter is surgical).
    assert r['projected'] is True and r['content'] == 'final answer', r


if __name__ == '__main__':
    print(_run_part1(True))
    print(_run_part1(False))
    print(_run_part2(False))
    print(_run_part2(True))
