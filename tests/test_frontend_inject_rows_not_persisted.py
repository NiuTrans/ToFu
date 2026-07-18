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

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CONV_JS = os.path.join(ROOT, 'static', 'js', 'core', 'conversations.js')


def _extract_trim_fn() -> str:
    """Pull the `_USAGE_TRANSIENT_KEYS` const + `_stripUsageTransient` +
    `_trimMsgForPersist` out of conversations.js so they eval standalone
    (module-private, not on window). Same extraction contract as
    tests/test_frontend_segments_not_echoed.py."""
    src = open(CONV_JS, encoding='utf-8').read()
    start = src.index('const _USAGE_TRANSIENT_KEYS')
    end = src.index('\nfunction _trimMsgForPersist(')
    end = src.index('\n}', end) + 2  # close of _trimMsgForPersist
    chunk = src[start:end]
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
// NEUTER: strip the inbox-inject filter block from the extracted function, then
// prove the synthetic rows LEAK into the persist copy — the strip is load-bearing.
let trimSrc = process.env.TRIM_SRC;
const neutered = trimSrc.replace(
  /if \(Array\.isArray\(r\.toolRounds\)\s*\n\s*&& r\.toolRounds\.some\(\(rd\) => rd && \(rd\._inboxInject[\s\S]*?rd\._userSteerInject\)\)\) \};\s*\}/,
  '/* NEUTERED inject-row strip */');
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


def test_source_has_inject_row_strip():
    """Cheap source guard (runs even without node): the belt exists and lists
    all three lane markers (lock-step with segments/_types.SYNTHETIC_INBOX_MARKERS)."""
    src = open(CONV_JS, encoding='utf-8').read()
    assert re.search(r"rd\._inboxInject \|\| rd\._peerInject \|\| rd\._userSteerInject", src), \
        'the inbox-inject strip block is missing from _trimMsgForPersist'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        _src = _extract_trim_fn()
        print(_run_node(_HARNESS, _src))
        print(_run_node(_NC_HARNESS, _src))
        test_source_has_inject_row_strip()
        print('PASS test_source_has_inject_row_strip')
