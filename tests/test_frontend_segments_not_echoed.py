#!/usr/bin/env python3
"""The client PUT must NOT echo the backend-owned ``segments`` field.

WHY (epic pt_cb8f98b0cb9b47fb, step 3/4 follow-up bug)
------------------------------------------------------
The backend now OWNS ``task['segments']`` as the authoritative typed-timeline
source of truth — ``_sync_result_to_conversation`` re-derives and re-persists
it on every task finalization, and it is written onto the persisted assistant
message dict. The frontend does NOT consume it yet (the per-tool-timeline
render cutover is a deferred epic). A frontend audit found that
``syncConversationToServer`` (``core/conversations.js``) starts its per-message
map from the FULL message (``let r = m``) and only conditionally clones — so a
loaded ``segments`` array was passed straight back on the next full-conv PUT.
Two harms:
  • BLOAT — ``segments`` restates content+thinking+tool-result text, roughly
    doubling the assistant payload on every sync.
  • STALE ECHO — after a local mutation (regen / translate) that did NOT update
    ``segments``, the client would overwrite the server-fresh segments with an
    older copy (client clobbering the new SoT).

FIX
---
``_trimMsgForPersist`` (the frontend mirror of the server-side persist
sanitizer, which already strips ``_partialOutput`` / ``usage._wire_fp``) now
also drops ``segments`` — same contract: a backend-owned field the client PUT
must never re-inflate.

This test EXTRACTS the real shipped ``_trimMsgForPersist`` from
``static/js/core/conversations.js`` and evals it in node (the function is
module-private, not on ``window``), asserting:
  1. a message carrying ``segments`` has it stripped from the persist copy;
  2. the ORIGINAL live message object is NOT mutated (clone-only-when-trimming);
  3. unrelated fields (content/thinking/toolRounds) survive untouched;
  4. NC (byte-revert control): a copy of the function WITHOUT the strip block
     leaves ``segments`` in the output → proving the strip is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_DIR = os.path.join(ROOT, 'static', 'js', 'core')
IDB_JS = os.path.join(ROOT, 'static', 'js', 'idb-cache.js')


def _locate(symbol: str) -> str:
    """Find the ONE file under static/js/core/ defining ``function <symbol>(``.

    Anchored on the SYMBOL, never a file path. This cluster migrated once
    (conversations.js -> conv_persist_helpers.js, pt_3879f00e slice 3) and took
    the hard-coded-path version of this guard down with it. Three separately
    reportable states: none = implementation deleted (REAL regression);
    many = single source of truth copied; one = auto re-point.
    """
    pat = re.compile(r'^function\s+' + re.escape(symbol) + r'\s*\(', re.M)
    hits = [os.path.join(CORE_DIR, n) for n in sorted(os.listdir(CORE_DIR))
            if n.endswith('.js')
            and pat.search(open(os.path.join(CORE_DIR, n), encoding='utf-8').read())]
    if not hits:
        raise AssertionError(
            f'{symbol}() is not defined anywhere under static/js/core/ — the '
            f'implementation was removed. REAL regression: the belt that keeps '
            f'backend-owned `segments` out of the client PUT is gone.')
    if len(hits) > 1:
        raise AssertionError(
            f'{symbol}() defined in {len(hits)} files ({hits}) — single source '
            f'of truth duplicated; collapse before re-pointing.')
    return hits[0]


def _fn_span(src: str, name: str) -> str:
    """Full text of a top-level ``function <name>(...) {...}``, brace-balanced."""
    m = re.search(r'^function\s+' + re.escape(name) + r'\s*\(', src, re.M)
    assert m, f'{name}() vanished between locate and slice'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces while slicing {name}()')


def _extract_trim_fn() -> str:
    """Splice the shipped `_USAGE_TRANSIENT_KEYS` + `_stripUsageTransient` +
    `_trimMsgForPersist` so they eval standalone (module-private, not on
    window). Located by symbol; never by file path."""
    path = _locate('_trimMsgForPersist')
    src = open(path, encoding='utf-8').read()
    const_m = re.search(r'^const _USAGE_TRANSIENT_KEYS\s*=.*?;', src, re.M)
    assert const_m, '_USAGE_TRANSIENT_KEYS const missing from ' + path
    chunk = '\n'.join([
        const_m.group(0),
        _fn_span(src, '_stripUsageTransient'),
        _fn_span(src, '_trimMsgForPersist'),
    ])
    assert '_trimMsgForPersist' in chunk, 'extraction missed _trimMsgForPersist'
    return chunk


def _extract_strip_message_fn() -> str:
    """Pull the `_stripMessage` (+ its `_stripUsageObj`/`_stripToolRound`
    helpers + `_USAGE_TRANSIENT` const) out of idb-cache.js so they eval
    standalone (they live inside the ConvCache IIFE closure, not on window)."""
    src = open(IDB_JS, encoding='utf-8').read()
    start = src.index('var _USAGE_TRANSIENT = [')
    end = src.index('\n  function _stripMessage(')
    end = src.index('\n    return r;\n  }', end) + len('\n    return r;\n  }')
    chunk = src[start:end]
    assert '_stripMessage' in chunk, 'extraction missed _stripMessage'
    return chunk


_HARNESS = r"""
const trimSrc = process.env.TRIM_SRC;
eval(trimSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A realistic finalized assistant message carrying backend-owned segments.
const segments = [
  { type: 'thinking', text: 'reasoning', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'Let me search.', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'tc1', name: 'web_search', input: '{}', llmRound: 0,
    result: { content: 'hit', status: 'done' } },
  { type: 'text', text: 'The answer.', deliverable: true, terminal: true },
];
const live = {
  role: 'assistant', content: 'The answer.', thinking: 'reasoning',
  toolRounds: [{ toolCallId: 'tc1', toolName: 'web_search', status: 'done',
                 toolContent: 'hit', llmRound: 0 }],
  segments: segments,
  _msgId: 'm1', finishReason: 'stop',
};

const persisted = _trimMsgForPersist(live);

check('segments_stripped_from_persist', !('segments' in persisted));
check('live_object_not_mutated', Array.isArray(live.segments) && live.segments.length === 4);
check('content_survives', persisted.content === 'The answer.');
check('thinking_survives', persisted.thinking === 'reasoning');
check('toolRounds_survive', Array.isArray(persisted.toolRounds) && persisted.toolRounds.length === 1);
check('is_a_clone', persisted !== live);

// A message with NO segments must pass through unchanged (no needless clone
// churn beyond the other trims — here nothing to trim → same ref).
const plain = { role: 'assistant', content: 'hi', toolRounds: [] };
const plainOut = _trimMsgForPersist(plain);
check('no_segments_passthrough', plainOut === plain && !('segments' in plainOut));

console.log(out.join('\n'));
"""

_NC_HARNESS = r"""
// NEUTER: strip the `segments` delete block from the extracted function, then
// prove segments LEAKS into the persist copy — the strip is load-bearing.
let trimSrc = process.env.TRIM_SRC;
const neutered = trimSrc.replace(
  /if \('segments' in m\) \{[\s\S]*?delete r\.segments;\s*\}/,
  '/* NEUTERED segments strip */');
if (neutered === trimSrc) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const live = { role: 'assistant', content: 'x', toolRounds: [],
               segments: [{ type: 'text', text: 'y', deliverable: true }] };
const persisted = _trimMsgForPersist(live);
// With the strip neutered, segments LEAKS through → confirms the real strip
// is what removes it.
check('nc_segments_leaks_without_strip', 'segments' in persisted);
console.log(out.join('\n'));
"""


def _run_node(harness: str, trim_src: str) -> str:
    env = dict(os.environ, TRIM_SRC=trim_src)
    proc = subprocess.run(
        ['node', '-e', harness],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_segments_stripped_from_persist_copy():
    trim_src = _extract_trim_fn()
    out = _run_node(_HARNESS, trim_src)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'segments-strip failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_without_strip_segments_leaks():
    trim_src = _extract_trim_fn()
    out = _run_node(_NC_HARNESS, trim_src)
    assert 'PASS nc_segments_leaks_without_strip' in out, (
        'NC control failed — either the pattern did not match or segments did '
        'not leak without the strip:\n' + out)


_CACHE_HARNESS = r"""
const stripSrc = process.env.STRIP_SRC;
eval(stripSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// BOUNDED contract (step 5): segments are KEPT in the cache (the interleaved
// renderer reads them for order+prose), but a tool_use segment's `result`
// (the duplicated, potentially multi-MB tool output) is stripped — tool
// bodies render from toolRounds, never from segment.result.
const segments = [
  { type: 'thinking', text: 'reasoning', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'Let me search.', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'tc1', name: 'web_search', input: '{}', llmRound: 0,
    result: { content: 'X'.repeat(5000), status: 'done' } },
  { type: 'text', text: 'The answer.', deliverable: true, terminal: true },
];
const msg = {
  role: 'assistant', content: 'The answer.', thinking: 'reasoning',
  toolRounds: [{ toolCallId: 'tc1', toolName: 'web_search', status: 'done',
                 toolContent: 'hit' }],
  segments: segments, _msgId: 'm1', finishReason: 'stop',
};
const cached = _stripMessage(msg);
const tu = (cached.segments || []).find(function(s){ return s.type === 'tool_use'; });

check('segments_kept_in_cache', Array.isArray(cached.segments) && cached.segments.length === 4);
check('tool_use_result_stripped', tu && !('result' in tu));
check('tool_use_structure_kept', tu && tu.id === 'tc1' && tu.name === 'web_search');
check('prose_kept', cached.segments.some(function(s){ return s.type === 'thinking' && s.text === 'reasoning'; }));
check('no_5000char_bulk', JSON.stringify(cached.segments).indexOf('XXXXX') === -1);
check('live_object_not_mutated', 'result' in msg.segments[2] && msg.segments[2].result.content.length === 5000);
check('content_survives', cached.content === 'The answer.');
check('toolRounds_survive', Array.isArray(cached.toolRounds) && cached.toolRounds.length === 1);
console.log(out.join('\n'));
"""

_CACHE_NC_HARNESS = r"""
// NEUTER: remove the tool_use `result`-strip so the multi-MB result bulk rides
// into the cached copy, proving the strip is load-bearing (the OOM guard).
let stripSrc = process.env.STRIP_SRC;
const neutered = stripSrc.replace(/if \(s\.type === 'tool_use' && 'result' in s\) \{[\s\S]*?return o;\s*\}/, '/* NEUTERED */');
if (neutered === stripSrc) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const msg = { role: 'assistant', content: 'x', toolRounds: [],
              segments: [{ type: 'tool_use', id: 't', name: 'web_search', input: '{}',
                           result: { content: 'X'.repeat(5000), status: 'done' } }] };
const cached = _stripMessage(msg);
const tu = (cached.segments || [])[0];
// Without the strip, the multi-MB result rides into the cache.
check('nc_result_leaks_without_strip', tu && 'result' in tu && tu.result.content.length === 5000);
console.log(out.join('\n'));
"""


def _run_node_cache(harness: str, strip_src: str) -> str:
    env = dict(os.environ, STRIP_SRC=strip_src)
    proc = subprocess.run(
        ['node', '-e', harness],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_segments_dropped_from_idb_cache():
    strip_src = _extract_strip_message_fn()
    out = _run_node_cache(_CACHE_HARNESS, strip_src)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'idb-cache segments-strip failures:\n' + out
    assert out.count('PASS') >= 8, f'expected >=8 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_idb_cache_without_skip_segments_leaks():
    strip_src = _extract_strip_message_fn()
    out = _run_node_cache(_CACHE_NC_HARNESS, strip_src)
    assert 'PASS nc_result_leaks_without_strip' in out, (
        'NC control failed — pattern did not match or tool_use.result did not '
        'leak without the strip:\n' + out)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_scan_surface_report():
    """Print WHERE both guards are pointed before asserting anything.

    charter: a source-anchored guard must show its scan surface, or it can pass
    while pointed at nothing. The PUT-side cluster already moved file once.
    """
    path = _locate('_trimMsgForPersist')
    print('_trimMsgForPersist located in:', os.path.relpath(path, ROOT))
    chunk = _extract_trim_fn()
    print(f'spliced PUT-side: {len(chunk)} chars / {chunk.count(chr(10)) + 1} lines')
    cache = _extract_strip_message_fn()
    print(f'spliced cache-side: {len(cache)} chars / {cache.count(chr(10)) + 1} lines')


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_both_strip_sites_hold_by_behaviour():
    """Both belts asserted by RESULT, not by source literal.

    The old version regex-matched ``if ('segments' in m)`` in a hard-coded file
    and died on the slice-3 move — unable to distinguish drift from regression.
    Behaviour assertions survive any reformatting of the condition while still
    going red the moment either belt stops working.
    """
    put_out = _run_node(r"""
eval(process.env.TRIM_SRC);
const r = _trimMsgForPersist({ role: 'assistant', content: 'c', toolRounds: [],
  segments: [{ type: 'text', text: 'y', deliverable: true }] });
console.log((!('segments' in r) ? 'PASS ' : 'FAIL ') + 'put_side_strips_segments');
""", _extract_trim_fn())
    cache_out = _run_node_cache(r"""
eval(process.env.STRIP_SRC);
const c = _stripMessage({ role: 'assistant', content: 'x', toolRounds: [],
  segments: [{ type: 'tool_use', id: 't', name: 'web_search', input: '{}',
               result: { content: 'X'.repeat(5000), status: 'done' } }] });
const tu = (c.segments || [])[0];
console.log((Array.isArray(c.segments) && c.segments.length === 1 ? 'PASS ' : 'FAIL ')
            + 'cache_keeps_segment_structure');
console.log((tu && !('result' in tu) ? 'PASS ' : 'FAIL ') + 'cache_strips_tool_result');
""", _extract_strip_message_fn())
    out = put_out + '\n' + cache_out
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'a segments belt stopped working:\n' + out
    assert out.count('PASS') == 3, f'expected 3 checks, got:\n{out}'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        _src = _extract_trim_fn()
        print(_run_node(_HARNESS, _src))
        print(_run_node(_NC_HARNESS, _src))
        test_scan_surface_report()
        test_both_strip_sites_hold_by_behaviour()
        print('PASS test_both_strip_sites_hold_by_behaviour')
