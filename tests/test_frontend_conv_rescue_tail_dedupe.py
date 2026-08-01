"""tests/test_frontend_conv_rescue_tail_dedupe.py — rescue verdict is id-aware.

Incident (conv ms8bx7089s3268, epic pt_93ff22bd): the local array held a
same-id twin — [user0, aborted-fragment(tmp_196fedef), settled-answer
(tmp_196fedef)] — and the server held [user0, settled-answer]. The
preserve-merge (🛟 KEEPING local) judged the extra tail row "missing
server-side" and PUSHED THE WHOLE LOCAL ARRAY back, persisting the
aborted fragment as a second entry with the same _msgId into the DB
(13:21:50, the RENDER ORDER VIOLATION incident's persistence leg).

A genuinely lost row's id is by definition NOT on the server; a rescued
tail row whose _msgId already exists server-side is a DUPLICATE, never a
rescue. The verdict now dedups on server-side ids. Driven against the
REAL leaf under jsdom (skips cleanly when node+jsdom are absent).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_frontend_conv_rescue_tail_dedupe.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;
eval(fs.readFileSync(path.join(ROOT, 'static/js/core/conv_rescue_tail.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const U = (id, c) => ({ role: 'user', _msgId: id, content: c || 'q' });
const A = (id, c, fr) => ({ role: 'assistant', _msgId: id, content: c || 'a', finishReason: fr });

// ── THE incident shape: local same-id twin, server holds the settled one ──
const local = [
  U('u0'),
  A('dup', 'ABORTED fragment', 'aborted'),
  A('dup', 'full settled answer', 'stop'),
];
const server = [U('u0'), A('dup', 'full settled answer', 'stop')];
const verdict = _rescuableLocalTail(local, server);
check('incident_twin_not_rescued', verdict.length === 0);

// ── A genuinely lost row (its id is NOT on the server) IS rescued ──
const local2 = [U('u0'), A('a1', 'answer'), A('lost-vu', 'vu turn'), ];
const server2 = [U('u0'), A('a1', 'answer')];
const verdict2 = _rescuableLocalTail(local2, server2);
check('genuine_lost_row_rescued', verdict2.length === 1 && verdict2[0]._msgId === 'lost-vu');

// ── A half-built optimistic draft (no id) is never rescued ──
const local3 = [U('u0'), A('a1', 'answer'), { role: 'assistant', content: 'draft' }];
const verdict3 = _rescuableLocalTail(local3, server2);
check('draft_not_rescued', verdict3.length === 0);

// ── Virtual-user rows keep their rescue (identity via _isVirtualUser) ──
const local4 = [U('u0'), A('a1', 'answer'), { role: 'user', content: 'vu', _isVirtualUser: true, _msgId: 'vu-1' }];
const verdict4 = _rescuableLocalTail(local4, server2);
check('vu_row_rescued', verdict4.length === 1);

// ── Server longer → never rescues (existing contract, unchanged) ──
check('server_longer_never_rescues', _rescuableLocalTail(server2, local2).length === 0);

console.log(out.join('\n'));
process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
"""


@pytest.mark.unit
def test_rescue_verdict_dedup():
    if not _node_deps_available():
        pytest.skip('node/jsdom not installed')
    probe = os.path.join(ROOT, 'node_modules', '.tmp_rescue_tail_harness.js')
    with open(probe, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        p = subprocess.run(['node', probe, ROOT], capture_output=True,
                           text=True, timeout=120)
    finally:
        os.unlink(probe)
    output = p.stdout + p.stderr
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'rescue-tail dedupe harness failures:\n' + output


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
