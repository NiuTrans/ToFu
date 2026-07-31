"""tests/test_frontend_conv_window.py — client half of windowed conversation
reads (static/js/conv_window.js).

Verifies the strangler-fig invariant + the scroll-up pagination logic:
  1. recordWindowState — a LEGACY full response (no windowed flag) is a no-op
     (conv._windowed false, no cursors); a windowed response stamps
     _windowed/_totalCount/_firstLoadedSeq/_hasMoreEarlier.
  2. loadEarlierMessages — fetches before_seq=firstLoadedSeq, PREPENDS the
     earlier page, advances the cursor, updates hasMore, re-renders.
  3. NC — with recordWindowState neutered (never sets _windowed), loadEarlier
     is inert (convHasMoreEarlier false) → proves the state stamp is
     load-bearing for pagination.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_SRC = os.path.join(ROOT, 'static', 'js', 'conv_window.js')


def _node():
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let getCalls = [];
let repaintCalls = [];
let serverResp = null;
global.debugLog = () => {};
global.conversations = [];
global.activeConvId = null;
/* The repaint collaborator conv_window.js actually calls. Stubbing the OLD
 * `renderChat` name left the real call throwing inside the try{}, which the
 * catch swallowed into `return 0` — the guard then reported a pagination
 * failure that did not exist. Located by symbol below so a future rename is
 * reported instead of silently mis-stubbed. */
global.ConvView = { replaceAll: (id, opts) => repaintCalls.push(id) };
global.document = { getElementById: () => null };  // no chatInner in this harness
global.Api = { conversations: { get: async (id, opts) => { getCalls.push([id, opts]); return serverResp; } } };

(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

function reset() {
  getCalls = []; repaintCalls = []; serverResp = null;
  global.conversations = []; global.activeConvId = null; global.window.TOFU_CONV_WINDOW = 60;
}

async function run() {
  // 1a. legacy full response → no-op
  reset();
  const legacy = { id: 'c1', messages: [{role:'user'}], updatedAt: 1 };
  const conv1 = { id: 'c1', messages: [] };
  const w1 = recordWindowState(conv1, legacy);
  check('legacy response: recordWindowState returns false', w1 === false);
  check('legacy response: conv not windowed', conv1._windowed === false);
  check('legacy response: convHasMoreEarlier false', convHasMoreEarlier(conv1) === false);

  // 1b. windowed response → stamps state
  reset();
  const conv2 = { id: 'c2', messages: [] };
  const w2 = recordWindowState(conv2, {
    id:'c2', windowed:true, totalCount:200, firstLoadedSeq:180, lastLoadedSeq:199,
    hasMore:true, messages:[{role:'user',content:'q180'}],
  });
  check('windowed: recordWindowState returns true', w2 === true);
  check('windowed: _windowed set', conv2._windowed === true);
  check('windowed: _totalCount', conv2._totalCount === 200);
  check('windowed: _firstLoadedSeq', conv2._firstLoadedSeq === 180);
  check('windowed: convHasMoreEarlier true', convHasMoreEarlier(conv2) === true);

  // 2. loadEarlierMessages prepends + advances cursor
  reset();
  const conv3 = { id:'c3', messages:[{role:'user',content:'q180',seq:180}], _windowed:true,
                  _firstLoadedSeq:180, _hasMoreEarlier:true };
  global.conversations = [conv3];
  global.activeConvId = 'c3';
  serverResp = { id:'c3', windowed:true, firstLoadedSeq:160, hasMore:true,
                 messages:[{role:'user',content:'q160'},{role:'assistant',content:'a160'}] };
  const n = await loadEarlierMessages('c3');
  check('loadEarlier: fetched before_seq=180', getCalls.length === 1 && getCalls[0][1].query.before_seq === '180');
  check('loadEarlier: prepended 2 earlier', n === 2 && conv3.messages.length === 3);
  check('loadEarlier: earlier is now first', conv3.messages[0].content === 'q160');
  check('loadEarlier: original tail preserved last', conv3.messages[2].content === 'q180');
  check('loadEarlier: cursor advanced to 160', conv3._firstLoadedSeq === 160);
  check('loadEarlier: re-rendered active conv', repaintCalls.length === 1);

  // 2b. loadEarlier stops when hasMore False
  reset();
  const conv4 = { id:'c4', messages:[{role:'user',seq:0}], _windowed:true, _firstLoadedSeq:0, _hasMoreEarlier:false };
  global.conversations = [conv4]; global.activeConvId = 'c4';
  const n4 = await loadEarlierMessages('c4');
  check('loadEarlier: no fetch when hasMore false', getCalls.length === 0 && n4 === 0);

  // 3. NC — recordWindowState never stamps windowed → pagination inert
  reset();
  const conv5 = { id:'c5', messages:[{role:'user'}] };
  // simulate neuter: do NOT call recordWindowState (or it never sets _windowed)
  check('NC: unstamped conv → convHasMoreEarlier false', convHasMoreEarlier(conv5) === false);
  global.conversations = [conv5]; global.activeConvId = 'c5';
  const n5 = await loadEarlierMessages('c5');
  check('NC: loadEarlier inert without window state', getCalls.length === 0 && n5 === 0);

  console.log(out.join('\n'));
}
run();
"""


def _run(js_path):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(_HARNESS)
        h = f.name
    try:
        r = subprocess.run(['node', h, js_path], capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr
    finally:
        os.unlink(h)


def _repaint_collaborator(js_src: str) -> str:
    """Return the repaint collaborator conv_window.js actually calls.

    The harness has to STUB that collaborator; stubbing a stale name makes the
    real call throw inside loadEarlierMessages' try{}, whose catch turns it into
    `return 0` — producing a pagination failure that does not exist. (That is
    exactly how this suite went red: it stubbed `renderChat`, which still exists
    elsewhere in the codebase but is not what this module calls.) Derive the name
    from the shipped source so a future rename is REPORTED, not mis-stubbed.
    """
    names = set(re.findall(r'window\.(\w+)\.\w+\(convId', js_src))
    if not names:
        raise AssertionError(
            'conv_window.js no longer calls any window.<X>.<m>(convId) repaint '
            'collaborator — re-point this guard after checking whether the '
            'repaint was removed on purpose')
    if len(names) > 1:
        raise AssertionError(f'multiple repaint collaborators {names}; '
                             'the harness can only stub a single one')
    return names.pop()


@pytest.mark.skipif(not _node(), reason='node not available')
def test_scan_surface_harness_stubs_the_real_collaborator():
    """Print the scan surface, then assert the harness stub MATCHES production.

    charter: verify what the guard is actually pointed at before trusting its
    green. Without this, the harness can drift from the module again and the
    resulting red looks like a product bug.
    """
    js = open(JS_SRC, encoding='utf-8').read()
    collaborator = _repaint_collaborator(js)
    print('conv_window.js repaints via: window.%s.replaceAll(convId, ...)' % collaborator)
    stubbed = set(re.findall(r'^global\.(\w+) = \{', _HARNESS, re.M))
    print('harness stubs:', sorted(stubbed))
    assert collaborator in stubbed, (
        f'harness does not stub the real repaint collaborator '
        f'{collaborator!r} (stubs: {sorted(stubbed)}) — the real call would '
        f'throw into loadEarlierMessages\' catch and be misread as a '
        f'pagination failure')


@pytest.mark.skipif(not _node(), reason='node not available')
def test_conv_window_client():
    out = _run(JS_SRC)
    lines = [ln for ln in out.splitlines() if ln.startswith(('PASS ', 'FAIL '))]
    assert lines, f'no results:\n{out}'
    failed = [ln for ln in lines if ln.startswith('FAIL ')]
    assert not failed, 'conv_window failures:\n' + '\n'.join(lines) + '\n\nRAW:\n' + out
    print('\n'.join(lines))
