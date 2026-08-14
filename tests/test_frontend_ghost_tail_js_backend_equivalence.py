"""tests/test_frontend_ghost_tail_js_backend_equivalence.py — equivalence guard
for Fix ③ of the empty-"Agent" air-bubble root fix: the FRONTEND in-session
ghost-tail self-heal.

WHY
---
``finishStream`` now settles the trailing assistant IN-SESSION by applying the
JS port ``_classifyGhostTailJS`` (static/js/ui/chat_render.js) — a 'delete'
splices a bare empty husk, an 'interrupt' stamps finishReason='interrupted' on
a thinking-only husk. That port MUST stay byte-equivalent to the backend
authority ``lib.conversations.reconcile.classify_ghost_tail`` — otherwise the
frontend would settle a turn differently than the DB does on the next reload,
re-introducing the very drift the backend reconcile was built to remove.

This suite runs the REAL ``_classifyGhostTailJS`` (via node) over a corpus of
trailing-assistant shapes and asserts it returns the SAME verdict the Python
``classify_ghost_tail`` returns for each. Verdict-token note: the JS returns
'interrupt' (matching the backend token); both apply the identical mutation.

CHECKS (GREEN when the port matches the backend)
  For every corpus row: JS verdict == backend verdict.

NEUTER CONTROL
  • nc_ignore_thinking: neuter the JS port to treat a thinking-only husk as a
    bare husk (return 'delete' where it should 'interrupt') → the thinking-only
    row DIVERGES from the backend → the equivalence assertion FAILS, proving the
    comparison has teeth.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Corpus of TRAILING-assistant shapes (verdict domain of classify_ghost_tail) ──
# Each entry: (name, message-kwargs). role='assistant', content='' unless set.
_CORPUS = [
    ('bare_empty', {}),
    ('thinking_only', {'thinking': 'reasoning...'}),
    ('whitespace_thinking', {'thinking': '   '}),
    ('has_content', {'content': 'hi'}),
    ('has_finish', {'finishReason': 'stop'}),
    ('has_usage', {'usage': {'input_tokens': 5}}),
    ('has_error', {'error': {'kind': 'internal'}}),
    ('real_tool_round_done', {'toolRounds': [{'status': 'done', 'toolName': 'x'}]}),
    ('real_tool_round_content', {'toolRounds': [{'status': 'searching', 'toolContent': 'o'}]}),
    ('real_tool_round_results', {'toolRounds': [{'status': 'searching', 'results': [{'r': 1}]}]}),
    ('empty_tool_round', {'toolRounds': [{'status': 'searching'}]}),
    ('special_vu', {'_isVirtualUser': True}),
    ('special_ep_planner', {'_isEndpointPlanner': True}),
    ('special_ep_review', {'_isEndpointReview': True}),
    ('special_ep_iter', {'_epIteration': 2}),
    ('ep_iter_zero_nonspecial', {'_epIteration': 0}),
    ('special_ig', {'_igResult': {'url': 'x'}}),
]


def _backend_verdict(kw: dict):
    from lib.conversations.reconcile import classify_ghost_tail
    msg = {'role': 'assistant', 'content': ''}
    msg.update(kw)
    return classify_ghost_tail(msg)


_HARNESS = r"""
const fs = require('fs');
const NC = process.argv[3] || '';
global.window = {};
let src = fs.readFileSync(process.argv[2], 'utf8');
const SRC = src;
if (NC === 'nc_ignore_thinking') {
  // Neuter: collapse the thinking-only 'interrupt' verdict to 'delete'.
  src = SRC.replace(
    'return (msg.thinking && String(msg.thinking).trim()) ? "interrupt" : "delete";',
    'return "delete"; // NC nc_ignore_thinking');
}
const applied = (NC === '') || (src !== SRC);
// Extract ONLY the three predicate fns + their window-expose block by evaling
// the whole file in a sandbox that stubs everything else it references at load.
// chat_render.js defines many fns but only _classifyGhostTailJS is called here,
// and it references nothing external at call time.
try {
  // Provide harmless stubs for anything referenced at TOP-LEVEL load.
  const _noop = function(){ return ''; };
  const sandbox = {};
  // eval in this scope; undefined globals throw only if used at load time.
  (0, eval)(src);
  const rows = JSON.parse(process.argv[4]);
  const out = {};
  out.__applied = applied;
  for (const [name, kw] of rows) {
    const msg = Object.assign({ role: 'assistant', content: '' }, kw);
    out[name] = (typeof _classifyGhostTailJS === 'function')
      ? _classifyGhostTailJS(msg) : '__no_fn__';
  }
  console.log(JSON.stringify(out));
} catch (e) {
  console.log(JSON.stringify({ __error: String(e && e.message || e) }));
}
"""


def _run_js(nc: str = ''):
    rows = json.dumps([[n, kw] for n, kw in _CORPUS])
    harness = os.path.join(HERE, f'_ghosttail_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, nc, rows],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert '__error' not in data, f'JS eval error: {data.get("__error")}'
    return data


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_js_ghost_tail_matches_backend():
    js = _run_js('')
    mismatches = {}
    for name, kw in _CORPUS:
        want = _backend_verdict(kw)
        got = js.get(name)
        # JS returns 'interrupt' | 'delete' | null; Python returns the same
        # tokens ('interrupt' | 'delete' | None). Normalise null↔None.
        got_n = None if got in (None, 'null') else got
        if got_n != want:
            mismatches[name] = {'js': got_n, 'backend': want, 'kw': kw}
    assert not mismatches, (
        '_classifyGhostTailJS DIVERGED from reconcile.classify_ghost_tail:\n'
        + json.dumps(mismatches, indent=2))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_ignore_thinking_regression_is_caught():
    """Neutering the JS thinking-tail branch (interrupt→delete) must diverge
    from the backend on the thinking-only row."""
    js = _run_js('nc_ignore_thinking')
    assert js.get('__applied') is True, f'NC did not apply:\n{js}'
    want = _backend_verdict({'thinking': 'reasoning...'})  # 'interrupt'
    got = js.get('thinking_only')
    got_n = None if got in (None, 'null') else got
    assert got_n != want, (
        'Neutering interrupt→delete did NOT diverge from the backend — the '
        f'equivalence check has no teeth (js={got_n} backend={want})')


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        test_js_ghost_tail_matches_backend()
        test_nc_ignore_thinking_regression_is_caught()
        print('PASS test_frontend_ghost_tail_js_backend_equivalence')
