#!/usr/bin/env python3
"""delta_reset(discard:true) — the canned-greeting retry reset semantics.

2026-08-02 incident: a user said "你好" to deepseek-v4-flash; the per-turn
tail injection had mutated the last user message (appended
<system-reminder> blocks), so the canned-greeting complement never fired
and the laconic — but LEGITIMATE — greeting reply was misclassified as the
upstream artifact. The retry then re-streamed the same greeting, and
because the canned bucket is the ONLY retry bucket whose discarded round
HAS content, each attempt concatenated onto the last: the bubble (and the
persisted message) showed the greeting three times.

The backend fix resets the round text and emits
``{'type': 'delta_reset', roundNum: N, discard: true}``. The reducer's
existing freeze guard CANNOT clear this case on its own: it only clears
once the prose is stamped onto a tool round of the same llmRound batch,
and a discarded canned round issued NO tool calls — there is no batch.
So ``discard: true`` is the explicit "clear unconditionally, keep tool
rounds" signal.

Two pins:
  1. discard:true clears content+thinking and KEEPS prior tool rounds.
  2. regression guard: WITHOUT discard, an unmatched delta_reset still
     KEEPS the prose (the original "frozen at a half word" freeze guard —
     the tool-narration path is untouched).

Run:  pytest tests/test_frontend_delta_reset_discard.py -v
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REDUCER_JS = runtime_section_path('ui/stream_reducer.js')


def _run_node(body: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=25)
        if out.returncode != 0:
            return {'ok': False, 'reason': 'node_error', 'stderr': out.stderr[-800:]}
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


def _harness(assert_body: str) -> str:
    return f'''
import * as fs from 'node:fs';
const REDUCER_PATH = {json.dumps(REDUCER_JS)};
let R = null;
try {{
  const src = fs.readFileSync(REDUCER_PATH, 'utf-8');
  const sandbox = {{}};
  const fn = new Function('exports', src + '\\n;' +
    'exports.projectStreamEvents = (typeof projectStreamEvents!=="undefined")?projectStreamEvents:null;' +
    'exports.reduceStreamState = (typeof reduceStreamState!=="undefined")?reduceStreamState:null;');
  fn(sandbox);
  R = sandbox;
}} catch (e) {{ R = {{ _err: String(e) }}; }}
if (!R || !R.projectStreamEvents) {{
  console.log(JSON.stringify({{ ok:false, reason:'reducer_missing', detail: R && R._err }}));
}} else {{
{assert_body}
}}
'''


_GREETING = '你好！有什么可以帮你的吗？'


def test_discard_clears_prose_and_keeps_tool_rounds():
    """The incident replay: round 0 did real tool work, round 1 streamed a
    greeting and is being discarded for retry (canned bucket). discard:true
    must clear the prose WITHOUT touching round 0's tool round, so the
    re-streamed attempt does not stack on top of the poisoned one."""
    body = _harness(f'''
  const events = [
    {{ type: 'round_start', roundNum: 0 }},
    {{ type: 'tool_start', roundNum: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' }},
    {{ type: 'tool_result', roundNum: 0, toolCallId: 'tc1', results: [{{toolName:'run_command',title:'ok',snippet:'a.py'}}] }},
    {{ type: 'tool_done', roundNum: 0, toolCallId: 'tc1', content: 'a.py' }},
    {{ type: 'round_start', roundNum: 1 }},
    {{ type: 'delta', thinking: '想了一下' }},
    {{ type: 'delta', content: {json.dumps(_GREETING)} }},
    {{ type: 'delta_reset', roundNum: 1, discard: true }},
    {{ type: 'delta', content: {json.dumps(_GREETING)} }},
  ];
  const s = R.projectStreamEvents(events);
  console.log(JSON.stringify({{ ok: true,
    content: s.content, thinking: s.thinking,
    rounds: (s.toolRounds || []).length,
    roundContent: (s.toolRounds[0] || {{}}).toolContent || null }}));
''')
    r = _run_node(body)
    assert r.get('ok'), r
    assert r['content'] == _GREETING, (
        f"discard reset failed — greeting stacked: {r['content']!r}")
    assert r['thinking'] == '', f"thinking not cleared: {r['thinking']!r}"
    assert r['rounds'] == 1 and r['roundContent'] == 'a.py', (
        f'discard must KEEP prior tool rounds: {r!r}')


def test_no_discard_freeze_guard_still_keeps_prose():
    """REGRESSION PIN: a plain delta_reset (no discard) whose round has NO
    matching tool round must still KEEP the prose — the freeze guard that
    protects inter-round narration until its tool_start lands. If this ever
    flips, the tool-narration path loses prose on reordered frames."""
    body = _harness('''
  const events = [
    { type: 'round_start', roundNum: 0 },
    { type: 'delta', content: 'Now let me check the logs.' },
    { type: 'delta_reset', roundNum: 0 },
  ];
  const s = R.projectStreamEvents(events);
  console.log(JSON.stringify({ ok: true, content: s.content }));
''')
    r = _run_node(body)
    assert r.get('ok'), r
    assert r['content'] == 'Now let me check the logs.', (
        f"freeze guard broken — unmatched plain delta_reset must KEEP prose: "
        f"{r['content']!r}")


if __name__ == '__main__':
    for fn in (test_discard_clears_prose_and_keeps_tool_rounds,
               test_no_discard_freeze_guard_still_keeps_prose):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:200])
