"""Guard: a server reply with FEWER messages must not overwrite a fuller local copy.

WHY
---
``loadConversationMessages`` treats the server as authoritative and assigns
``conv.messages = serverMsgs``. That is wrong when the server holds fewer rows
than the client already has: a backend whole-blob writer that lost a race can
erase a row that was already committed (measured incident, conv ms3sfyrmn31omb
2026-07-28 — 13 ``Appended VU msg`` log lines, 8 surviving rows), and at that
moment the local copy is the ONLY place the message still exists. Overwriting
there is what destroys it for good.

The branch used to detect exactly this, log a warning, and overwrite anyway — a
gate that reports the break-in instead of closing the door. Its own comment
called it the birthplace of the chatInner-disappearance bug and said the
postmortem starts there. It recurred.

WHAT IS ASSERTED (results, not implementation)
----------------------------------------------
The decision lives in the pure seam ``_rescuableLocalTail(localMsgs,
serverMsgs)``, which the shipped gate calls. Driving the REAL shipped function
(resolved BY SYMBOL through the bundler's own manifest, never a hard-coded path
and never a hand-copied twin):

1. Server missing an identified local row (``_msgId`` / ``_isVirtualUser``)
   → that row is reported rescuable, so the gate keeps + pushes back.
2. Complement — a local draft with NO identity is NOT rescuable, so the normal
   overwrite still happens. Without this, "keep everything" would satisfy (1)
   while stranding drafts the server has legitimately never seen.
3. Equal/longer server → nothing rescuable (the ordinary adoption path).
4. The shipped gate actually CALLS the seam — a guard on a seam nothing uses is
   a guard on dead code.
5. NEUTER — a copy of the file with the identity filter removed must change the
   verdict, proving the filter is load-bearing. The shipped tree is untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._conv_bundle_sources import sources_defining  # noqa: E402

pytestmark = pytest.mark.unit

_SEAM = '_rescuableLocalTail'
_GATE = 'loadConversationMessages'


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _seam_source() -> str:
    """Absolute path of the shipped file defining the seam, resolved by SYMBOL.

    Hard-coding ``core/conversations.js`` is the anchor-drift failure this
    project keeps re-learning: the file has been decomposed repeatedly, so a
    path-anchored guard goes red for reasons that have nothing to do with the
    product. ``sources_defining`` asks the bundler's manifest instead.
    """
    return sources_defining(_SEAM)[0]


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Extract ONLY the seam function. The file is a browser bundle member that
// references window/document at load time, so eval'ing the whole thing under
// bare node would fail for reasons unrelated to this rule.
const m = src.match(/function _rescuableLocalTail\([\s\S]*?\n\}/);
if (!m) { console.log(JSON.stringify({error: 'SEAM_NOT_FOUND'})); process.exit(0); }
eval(m[0]);

const VU  = { role: 'user', content: 'VU reply', _isVirtualUser: true, _msgId: 'vu-1' };
const MSG = { role: 'assistant', content: 'a', _msgId: 'a1' };
const Q   = { role: 'user', content: 'q', _msgId: 'u1' };
const DRAFT = { role: 'user', content: 'half-typed' };   // no identity

const out = {
  // 1. server lost an identified row -> rescuable
  lostVu:      _rescuableLocalTail([Q, MSG, VU], [Q, MSG]).length,
  // 2. local draft with no id -> NOT rescuable (overwrite proceeds)
  draftOnly:   _rescuableLocalTail([Q, MSG, DRAFT], [Q, MSG]).length,
  // 3. ordinary adoption paths
  equalLen:    _rescuableLocalTail([Q, MSG], [Q, MSG]).length,
  serverLonger:_rescuableLocalTail([Q], [Q, MSG]).length,
  // mixed tail: only the identified row is rescued
  mixedTail:   _rescuableLocalTail([Q, DRAFT, VU], [Q]).length,
};
console.log(JSON.stringify(out));
"""


def _run(js_path: str) -> dict:
    harness = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '_conv_overwrite_gate_harness.js')
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    line = next((l for l in proc.stdout.splitlines() if l.strip().startswith('{')), None)
    assert line, f'harness produced no result:\n{proc.stdout}'
    return json.loads(line)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_a_row_the_server_lost_is_reported_rescuable():
    out = _run(_seam_source())
    assert out.get('error') != 'SEAM_NOT_FOUND', (
        f'{_SEAM} is gone from the shipped source — the gate it backs was '
        f'removed. Product regression, not harness drift.')
    assert out['lostVu'] == 1, (
        'a committed row missing from a shorter server reply was NOT reported '
        f'rescuable, so the gate overwrites and the only surviving copy of that '
        f'message is destroyed: {out}')
    assert out['mixedTail'] == 1, (
        f'the identified row in a mixed tail was not rescued: {out}')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_an_unidentified_local_draft_is_not_rescued():
    """Complement: "keep everything" must not pass the test above."""
    out = _run(_seam_source())
    assert out['draftOnly'] == 0, (
        'a local draft with no _msgId/_isVirtualUser was treated as rescuable — '
        '"never adopt the server" is as wrong as always adopting it, and would '
        f'strand drafts the server legitimately has not seen: {out}')
    assert out['equalLen'] == 0 and out['serverLonger'] == 0, (
        f'ordinary adoption paths were misreported as rescuable: {out}')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_the_shipped_gate_actually_calls_the_seam():
    """A seam nothing calls is dead code, and a guard on it proves nothing."""
    src = open(_seam_source(), encoding='utf-8').read()
    assert f'{_SEAM}(conv.messages, serverMsgs)' in src, (
        f'the shipped {_GATE} no longer routes its adoption decision through '
        f'{_SEAM} — this guard would still pass while the product overwrites.')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_the_identity_filter_is_load_bearing_neuter(tmp_path):
    """NEUTER: drop the identity filter on a COPY → the verdict must change.

    Removing the filter makes every extra local row 'rescuable', so the draft
    case flips from 0 to 1. If it does not, the assertions above are not
    measuring the filter at all. The shipped file is left byte-identical.
    """
    path = _seam_source()
    src = open(path, encoding='utf-8').read()
    marker = """  return localMsgs.slice(serverMsgs.length)
    .filter(m => m && (m._msgId || m._isVirtualUser));"""
    assert marker in src, ('the identity filter anchor drifted — the neuter '
                           'would not bite. Re-point it before trusting this.')
    neutered = src.replace(marker, '  return localMsgs.slice(serverMsgs.length);', 1)
    assert neutered != src, 'neuter changed nothing'

    copy = tmp_path / 'conversations_neutered.js'
    copy.write_text(neutered, encoding='utf-8')
    out = _run(str(copy))

    assert out['draftOnly'] == 1, (
        'with the identity filter removed the draft was STILL not rescuable — '
        f'the complement assertion is not measuring this filter: {out}')

    assert open(path, encoding='utf-8').read() == src, (
        'the harness mutated the shipped source')
