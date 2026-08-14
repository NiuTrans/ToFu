#!/usr/bin/env python3
"""The client PUT must NOT persist synthetic inbox-inject rows into ``toolRounds``.

WHY (epic pt_d022c86a00fc4580 — "agent_inbox info disappears on refresh")
--------------------------------------------------------------------------
Swarm ``<swarm-update>`` / peer / user-steer injections are shown to the human
as an in-timeline synthetic ``toolRound`` (flagged ``_inboxInject`` /
``_peerInject`` / ``_userSteerInject``, ``roundNum`` 9e6+, NO ``toolCallId`` /
``toolContent``). The LIVE SSE handlers push these rows into the running
``conv.messages[].toolRounds`` so the chip appears the instant results land —
but their DURABLE home is the underscore sidecar (``_inboxInjects`` /
``_peerInjects`` / ``_userSteerInjects``); ``getToolRoundsFromMsg`` rebuilds the
synthetic rows at render time from that sidecar.

The danger: ``syncConversationToServer`` fires a FULL-CONV PUT of the live
``conv.messages`` — and if it lands mid-stream (before the terminal
``committedMessage`` overwrites ``toolRounds`` with the clean backend list), the
synthetic rows would be persisted into the DB ``toolRounds``. That array is ALSO
the wire-replay / prefix-cache source: a row lacking ``toolCallId`` /
``toolContent`` collapses the WHOLE assistant turn to a lossy summary (breaking
tool-turn continuation) AND shifts the wire prefix (cache miss). The server-side
``is_synthetic_inbox_round`` guard already filters them from the wire; this belt
keeps the DB blob itself clean so the two never diverge.

FIX
---
``_trimMsgForPersist`` (the frontend PUT sanitizer, which already strips
``segments`` / ``_partialOutput`` / ``usage._wire_fp``) now also drops any
``toolRounds`` entry flagged ``_inboxInject`` / ``_peerInject`` /
``_userSteerInject``, clone-and-strip (never mutating the live array).

This test EXTRACTS the real shipped ``_trimMsgForPersist`` from
``static/js/core/conversations.js`` and evals it in node, asserting:
  1. synthetic inject rows are stripped from the persist copy;
  2. GENUINE tool rounds survive untouched (byte-identical);
  3. the ORIGINAL live message object + its toolRounds array are NOT mutated;
  4. NC (byte-revert control): a copy of the function WITHOUT the strip block
     leaves the synthetic rows in the output → proving the strip is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_DIR = os.path.join(runtime_sections_dir(), 'core')


def _locate(symbol: str) -> str:
    """Find the ONE file under static/js/core/ defining ``function <symbol>(``.

    Anchored on the SYMBOL, never on a file path: this helper cluster has
    already migrated once (conversations.js -> conv_persist_helpers.js, slice 3
    of pt_3879f00e), which killed the previous hard-coded-path version of this
    guard. Three states are separately reportable so a future failure says what
    actually happened instead of "substring not found":
      none  -> the implementation was deleted (a REAL regression, not drift)
      many  -> the single source of truth got copied (collapse it first)
      one   -> re-point automatically
    """
    pat = re.compile(r'^function\s+' + re.escape(symbol) + r'\s*\(', re.M)
    hits = []
    for name in sorted(os.listdir(CORE_DIR)):
        if not name.endswith('.js'):
            continue
        path = os.path.join(CORE_DIR, name)
        if pat.search(open(path, encoding='utf-8').read()):
            hits.append(path)
    if not hits:
        raise AssertionError(
            f'{symbol}() is not defined anywhere under static/js/core/ — the '
            f'implementation was removed. This is a REAL regression: the belt '
            f'that keeps synthetic inject rows out of the DB toolRounds is gone. '
            f'Restore it before touching this guard.')
    if len(hits) > 1:
        raise AssertionError(
            f'{symbol}() defined in {len(hits)} files ({hits}) — the single '
            f'source of truth was duplicated; collapse it before re-pointing.')
    return hits[0]


def _fn_span(src: str, name: str) -> str:
    """Return the full text of a top-level ``function <name>(...) {...}``,
    balancing braces so nested blocks survive."""
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


_HARNESS = r"""
const trimSrc = process.env.TRIM_SRC;
eval(trimSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Two genuine tool rounds interleaved with the three synthetic inject lanes —
// exactly what the live SSE handlers leave on conv.messages[].toolRounds during
// a swarm/peer/steer turn.
const realA = { roundNum: 1, llmRound: 0, toolCallId: 'tc_1', toolName: 'web_search',
                toolArgs: '{"q":"gil"}', toolContent: 'hit', status: 'done' };
const realB = { roundNum: 2, llmRound: 1, toolCallId: 'tc_2', toolName: 'read_files',
                toolArgs: '{"path":"a.py"}', toolContent: 'body', status: 'done' };
const synthInbox = { roundNum: 9000001, status: 'done', _inboxInject: true,
                     _inboxKey: 'inbox:1', inboxRound: 1, inboxCount: 2,
                     inboxAgentIds: ['a1', 'a2'],
                     inboxPreviews: [{ agentId: 'a1', text: 'done A' }] };
const synthPeer = { roundNum: 9000002, status: 'done', _peerInject: true,
                    _peerKey: 'peer:2', peerRound: 2, peerCount: 1,
                    peerPreviews: [{ fromConv: 'sib1', text: 'peer note' }] };
const synthSteer = { roundNum: 9000003, status: 'done', _userSteerInject: true,
                     _steerKey: 'steer:2', steerRound: 2, steerCount: 1,
                     steerPreviews: [{ text: 'focus on X' }] };

const live = {
  role: 'assistant', content: 'The answer.', thinking: 'reasoning',
  toolRounds: [realA, synthInbox, realB, synthPeer, synthSteer],
  _inboxInjects: [{ round: 1, count: 2, agentIds: ['a1', 'a2'],
                   previews: [{ agentId: 'a1', text: 'done A' }] }],
  _peerInjects: [{ round: 2, count: 1, previews: [{ fromConv: 'sib1', text: 'peer note' }] }],
  _userSteerInjects: [{ round: 2, count: 1, previews: [{ text: 'focus on X' }] }],
  _msgId: 'm1', finishReason: 'stop',
};

const persisted = _trimMsgForPersist(live);

// 1. Synthetic rows gone; only the two genuine tool rounds remain.
check('synthetic_rows_stripped',
  Array.isArray(persisted.toolRounds) && persisted.toolRounds.length === 2);
check('no_inbox_row', !persisted.toolRounds.some(r => r._inboxInject));
check('no_peer_row', !persisted.toolRounds.some(r => r._peerInject));
check('no_steer_row', !persisted.toolRounds.some(r => r._userSteerInject));

// 2. Genuine rounds survive byte-identical (real wire-replay source intact).
check('real_rounds_survive',
  persisted.toolRounds[0].toolCallId === 'tc_1' &&
  persisted.toolRounds[1].toolCallId === 'tc_2');

// 3. The live object + its toolRounds array are NOT mutated (clone-only).
check('live_toolRounds_not_mutated', live.toolRounds.length === 5);
check('is_a_clone', persisted !== live && persisted.toolRounds !== live.toolRounds);

// 4. The sidecar (durable home) is preserved on the persist copy — the chips
//    survive reload; only the wire-poison rows are stripped from toolRounds.
check('sidecar_inbox_preserved',
  Array.isArray(persisted._inboxInjects) && persisted._inboxInjects.length === 1);
check('sidecar_peer_preserved',
  Array.isArray(persisted._peerInjects) && persisted._peerInjects.length === 1);
check('sidecar_steer_preserved',
  Array.isArray(persisted._userSteerInjects) && persisted._userSteerInjects.length === 1);

// A message with ONLY genuine rounds passes through without a needless
// toolRounds clone from this belt (nothing to strip → same array ref).
const clean = { role: 'assistant', content: 'hi', toolRounds: [realA] };
const cleanOut = _trimMsgForPersist(clean);
check('clean_toolRounds_passthrough', cleanOut.toolRounds === clean.toolRounds);

console.log(out.join('\n'));
"""

_NC_HARNESS = r"""
// NEUTER: disable the inject-row filter by making its marker test always false,
// then prove the synthetic rows LEAK into the persist copy — i.e. the strip is
// load-bearing. Neutralising the MARKER NAMES (rather than pattern-matching the
// whole block) survives reformatting of the block itself.
let trimSrc = process.env.TRIM_SRC;
const neutered = trimSrc.replace(
  /rd\._inboxInject \|\| rd\._peerInject \|\| rd\._userSteerInject/g, 'false');
if (neutered === trimSrc) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const live = {
  role: 'assistant', content: 'x',
  toolRounds: [
    { roundNum: 1, toolCallId: 'tc_1', toolName: 'web_search', toolArgs: '{}',
      toolContent: 'hit', status: 'done' },
    { roundNum: 9000001, status: 'done', _inboxInject: true, _inboxKey: 'inbox:1',
      inboxRound: 1, inboxCount: 1, inboxAgentIds: ['a1'], inboxPreviews: [] },
  ],
};
const persisted = _trimMsgForPersist(live);
// With the strip neutered, the synthetic inbox row LEAKS through → confirms the
// real strip is what keeps the DB toolRounds (and thus the wire) clean.
check('nc_synthetic_row_leaks_without_strip',
  persisted.toolRounds.length === 2 && persisted.toolRounds.some(r => r._inboxInject));
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
def test_synthetic_inject_rows_stripped_from_persist_copy():
    trim_src = _extract_trim_fn()
    out = _run_node(_HARNESS, trim_src)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'inject-row-strip failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_without_strip_synthetic_rows_leak():
    trim_src = _extract_trim_fn()
    out = _run_node(_NC_HARNESS, trim_src)
    assert 'PASS nc_synthetic_row_leaks_without_strip' in out, (
        'NC control failed — either the pattern did not match or the synthetic '
        'rows did not leak without the strip:\n' + out)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_scan_surface_report():
    """Print WHERE the guard is currently pointed before asserting anything.

    charter: a source-anchored guard must show its scan surface, or it can pass
    while pointed at nothing. This cluster already moved file once.
    """
    path = _locate('_trimMsgForPersist')
    print('_trimMsgForPersist located in:', os.path.relpath(path, ROOT))
    chunk = _extract_trim_fn()
    print(f'spliced {len(chunk)} chars / {chunk.count(chr(10)) + 1} lines')
    for marker in ('_inboxInject', '_peerInject', '_userSteerInject'):
        print(f'  marker {marker}: {chunk.count(marker)} occurrence(s)')


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_every_inject_lane_is_actually_filtered():
    """Each of the three lanes must be stripped INDIVIDUALLY.

    Asserts the RESULT (a row bearing lane marker X does not survive the persist
    copy), not the presence of a source literal — so reformatting or reordering
    the condition cannot produce a false red, while dropping a lane goes red.
    Replaces the old literal-regex source guard, which died when this cluster
    moved to conv_persist_helpers.js and could not tell drift from regression.
    """
    harness = r"""
eval(process.env.TRIM_SRC);
const out = [];
for (const marker of ['_inboxInject', '_peerInject', '_userSteerInject']) {
  const real = { roundNum: 1, toolCallId: 'tc_1', toolName: 'x',
                 toolContent: 'y', status: 'done' };
  const synth = { roundNum: 9000001, status: 'done' };
  synth[marker] = true;
  const res = _trimMsgForPersist({ role: 'assistant', content: 'c',
                                   toolRounds: [real, synth] });
  const kept = res.toolRounds.length === 1 && res.toolRounds[0].toolCallId === 'tc_1';
  out.push((kept ? 'PASS ' : 'FAIL ') + 'lane_stripped:' + marker);
}
console.log(out.join('\n'));
"""
    out = _run_node(harness, _extract_trim_fn())
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'a synthetic inject lane reaches the DB toolRounds:\n' + out
    assert out.count('PASS') == 3, f'expected 3 lanes checked, got:\n{out}'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        _src = _extract_trim_fn()
        print(_run_node(_HARNESS, _src))
        print(_run_node(_NC_HARNESS, _src))
        test_scan_surface_report()
        test_every_inject_lane_is_actually_filtered()
        print('PASS test_every_inject_lane_is_actually_filtered')
