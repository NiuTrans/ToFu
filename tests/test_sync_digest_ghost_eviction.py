"""Guard: sync-digest ghost eviction removes server-deleted conversations.

THE DEFECT (measured in production, 2026-08-19)
-----------------------------------------------
Conversation ``msy954np`` was DELETE'd on one tab at 00:44. A second tab
kept it in localStorage and reported it on every 60s sync-digest probe; the
server logged ``kind=unknown_conv`` 154 times that day and NEVER answered
with a correction — the ghost sidebar entry lived forever, and every open
attempt 404'd.

THE FIX
-------
The server names such convs in ``deletedConvIds`` (only when the authority
read demonstrably succeeded), and the shipped
``_applySyncDigestGhostEvictions`` runs the same eviction as the
loadConvMsgs 404-ghost path: cache remove, in-place array removal,
sidebar rerender, and a fresh chat when the ghost was open.

These probes drive the REAL shipped function under jsdom — not a
reimplementation — so a regression in the shipped file fails here.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_sync_digest_ghost_eviction.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

REDUCER = runtime_section_path('core/conv_state_reducer.js')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

// Seams the section reads, provided at module/global scope.
let cacheRemoved = [];
let renders = 0;
let newChats = 0;
window.ConvCache = { remove: (id) => { cacheRemoved.push(id); } };
window.renderConversationList = () => { renders++; };
window.newChat = () => { newChats++; };
window.activeConvId = 'cv-keep';

async function main() {
  // Scenario 1: ghost among keepers — removed in place, cache dropped,
  // sidebar rerendered once, no newChat (the ghost is not open).
  let conversations = [{ id: 'cv-ghost' }, { id: 'cv-keep' }, null];
  const arrayIdentity = conversations;
  let dropped = _applySyncDigestGhostEvictions(conversations, ['cv-ghost']);
  const s1 = {
    dropped,
    sameArray: conversations === arrayIdentity,
    remaining: conversations.map((c) => c && c.id),
    cacheRemoved: cacheRemoved.slice(),
    renders, newChats,
  };

  // Scenario 2: the OPEN conv is a ghost — evicted AND a fresh chat starts.
  cacheRemoved = []; renders = 0; newChats = 0;
  window.activeConvId = 'cv-ghost';
  conversations = [{ id: 'cv-ghost' }, { id: 'cv-keep' }];
  dropped = _applySyncDigestGhostEvictions(conversations, ['cv-ghost']);
  const s2 = { dropped, remaining: conversations.map((c) => c && c.id),
               newChats, renders };

  // Scenario 3: unknown ids / malformed input are no-ops (no rerender, no
  // cache writes, no crash).
  cacheRemoved = []; renders = 0; newChats = 0;
  conversations = [{ id: 'cv-keep' }];
  dropped = _applySyncDigestGhostEvictions(
    conversations, ['cv-absent', '', 42, null]);
  const s3 = { dropped, remaining: conversations.map((c) => c && c.id),
               cacheRemoved: cacheRemoved.slice(), renders, newChats };
  const s4 = {
    notArrays: _applySyncDigestGhostEvictions(null, ['x']) === 0
      && _applySyncDigestGhostEvictions([], null) === 0,
  };

  console.log(JSON.stringify({ s1, s2, s3, s4 }));
}
main().catch((e) => { console.error(e); process.exit(1); });
"""


def _have_node() -> bool:
    return shutil.which('node') is not None


def _run_probes() -> dict:
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(_HARNESS)
        harness = handle.name
    try:
        proc = subprocess.run(
            ['node', harness, REDUCER],
            capture_output=True, text=True, timeout=60, check=False)
    finally:
        os.unlink(harness)
    assert proc.returncode == 0, f'node harness failed:\n{proc.stderr}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _have_node(), reason='node is required')
def test_ghost_eviction_contract():
    out = _run_probes()

    s1 = out['s1']
    assert s1['dropped'] == 1
    assert s1['sameArray'] is True, (
        'must mutate the shared module-level array in place')
    assert s1['remaining'] == ['cv-keep', None], (
        'the ghost is removed; keepers (incl. a null hole) are untouched')
    assert s1['cacheRemoved'] == ['cv-ghost']
    assert s1['renders'] == 1
    assert s1['newChats'] == 0

    s2 = out['s2']
    assert s2['dropped'] == 1
    assert s2['remaining'] == ['cv-keep']
    assert s2['newChats'] == 1, 'an OPEN ghost must land the user on a fresh chat'
    assert s2['renders'] == 1

    s3 = out['s3']
    assert s3['dropped'] == 0
    assert s3['remaining'] == ['cv-keep']
    assert s3['cacheRemoved'] == []
    assert s3['renders'] == 0 and s3['newChats'] == 0
    assert out['s4']['notArrays'] is True


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
