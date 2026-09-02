#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — locateRound canonical-key guard (owner item ③).

After the events.py wire unification (§5 — every round-bearing event now emits
the canonical ``roundNum`` key), the reducer's ``locateRound`` must STOP needing
its old four-name fallback (roundNum / round / llmRound / synthetic). This guard
proves the drift can no longer hide:

  1. An event carrying ONLY the canonical ``roundNum`` locates its round.
  2. An event carrying ONLY a stale ``round`` key (the retired wire alias) does
     NOT silently locate a round — a mismatched key must MISS, so a future
     regression that re-introduces a bare ``round`` emit surfaces as a broken
     locate (errors can't hide) instead of being silently absorbed.
  3. ``toolCallId`` still wins when present (conversation-unique addressing).
  4. ``llmRound`` remains valid ONLY as the round-OBJECT batch-grouping key —
     an event addressing a round by a bare ``llmRound`` field also does not
     silently locate (it is not a wire index key).

Extraction-and-eval in node, mirroring tests/test_frontend_reducer_parity.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root
ROOT = _legacy_test_root()
REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_reducer.js')


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
const src = fs.readFileSync({json.dumps(REDUCER_JS)}, 'utf-8');
const sandbox = {{}};
const fn = new Function('exports', src + '\\n;' +
  'exports.locateRound = (typeof locateRound!=="undefined")?locateRound:null;' +
  'exports.reduceStreamState = (typeof reduceStreamState!=="undefined")?reduceStreamState:null;');
fn(sandbox);
const R = sandbox;
if (!R.locateRound) {{ console.log(JSON.stringify({{ok:false, reason:'no_locateRound'}})); }}
else {{
{assert_body}
}}
'''


def test_canonical_roundNum_locates_and_stale_round_does_not():
    """Canonical roundNum locates; a stale bare `round` key does NOT silently
    match (the drift the unification removed can no longer hide)."""
    body = _harness('''
  const rounds = [
    { roundNum: 1, llmRound: 0, toolCallId: 'tc1', status: 'done' },
    { roundNum: 2, llmRound: 1, toolCallId: 'tc2', status: 'searching' },
  ];
  // 1. canonical roundNum-only event locates round 2.
  const byCanonical = R.locateRound(rounds, { roundNum: 2 });
  // 2. stale `round`-only event (retired wire alias) must MISS — not silently
  //    resolve to round 2.
  const byStaleRound = R.locateRound(rounds, { round: 2 });
  // 3. toolCallId wins when present.
  const byId = R.locateRound(rounds, { toolCallId: 'tc1' });
  // 4. a bare llmRound event field does not locate (llmRound is a round-OBJECT
  //    batch key, not a wire index key).
  const byBareLlmRound = R.locateRound(rounds, { llmRound: 1 });
  console.log(JSON.stringify({
    ok: true,
    canonicalLocates: !!byCanonical && byCanonical.roundNum === 2,
    staleRoundMisses: byStaleRound == null,
    idWins: !!byId && byId.toolCallId === 'tc1',
    bareLlmRoundMisses: byBareLlmRound == null,
  }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f'reducer/node error: {r.get("reason")} {r.get("stderr","")}')
    assert r['canonicalLocates'], (
        'a canonical roundNum-only event must locate its round')
    assert r['staleRoundMisses'], (
        'DRIFT CAN STILL HIDE: an event carrying only a stale bare `round` key '
        'silently located a round — locateRound still absorbs the retired wire '
        'alias. The wire is unified on roundNum, so a bare `round` event must '
        'MISS (surfacing a regression) rather than be silently matched.')
    assert r['idWins'], 'toolCallId must still win when present'
    assert r['bareLlmRoundMisses'], (
        'a bare llmRound event field must not silently locate — llmRound is a '
        'round-object batch-grouping key, not a wire index key')


def test_tool_result_with_canonical_roundNum_settles_the_round():
    """End-to-end through reduceStreamState: a tool_result carrying only the
    canonical roundNum settles the matching round; one carrying only a stale
    `round` key does NOT settle it (stays 'searching')."""
    body = _harness('''
  // Build a round via tool_start (canonical roundNum), then settle it two ways.
  function freshState() {
    let s = { content:'', thinking:'', toolRounds:[] };
    s = R.reduceStreamState(s, { type:'tool_start', roundNum: 5, toolName:'x' });
    return s;
  }
  // (a) canonical roundNum result settles it.
  const sA = freshState();
  R.reduceStreamState(sA, { type:'tool_result', roundNum: 5, results:[{title:'r'}] });
  const settledByCanonical = sA.toolRounds[0].status === 'done';
  // (b) stale `round`-only result must NOT settle it (no toolCallId either).
  const sB = freshState();
  R.reduceStreamState(sB, { type:'tool_result', round: 5, results:[{title:'r'}] });
  const notSettledByStale = sB.toolRounds[0].status === 'searching';
  console.log(JSON.stringify({ ok:true, settledByCanonical, notSettledByStale }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f'reducer/node error: {r.get("reason")} {r.get("stderr","")}')
    assert r['settledByCanonical'], (
        'a tool_result with the canonical roundNum must settle its round')
    assert r['notSettledByStale'], (
        'a tool_result carrying only a stale `round` key must NOT silently '
        'settle the round — the canonical-key requirement makes the drift '
        'visible instead of absorbed')


if __name__ == '__main__':
    for fn in (test_canonical_roundNum_locates_and_stale_round_does_not,
               test_tool_result_with_canonical_roundNum_settles_the_round):
        try:
            fn(); print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  FAIL', fn.__name__, '::', str(e)[:200])
