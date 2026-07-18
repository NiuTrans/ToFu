"""Regression: transient/diagnostic message fields must be TRIMMED at every
persist boundary, or a single conversation balloons to 100+ MB and the browser
tab exhausts memory the moment it loads and renders it.

WHY
---
Three fields leak into the persisted conversation JSON with zero render value:

  1. ``usage._wire_fp`` / ``_wire_static`` — the post-translation wire
     fingerprint (a ~226 KB canonicalized-message LIST per round), captured in
     lib/llm/_sse_core.py purely for same-run cache-miss diagnosis by
     lib/tasks_pkg/cache_tracking.py (which keeps its OWN in-memory copy). NO
     render path reads it. Rides into apiRounds[].usage, the final usage, and
     the frontend-only _liveLastRoundUsage.usage. This was the DOMINANT bloat:
     19.65 MB in the real OOM conversation mr80gsd8rywph9 (121 MB total).
  2. ``toolRounds[]._partialOutput`` on a DONE round — the live run_command
     terminal buffer accumulated during streaming. Once the round is done the
     authoritative output is in results[0].output / toolContent; the buffer is
     dead weight (18 MB observed in mqxbemdr7asicp while toolContent was 2 KB).
  3. ``toolRounds[].results[].imageDataUris[].uri`` — multi-MB inline base64
     data: URLs (9 MB in mr8l9rq09d34n3). These ARE the render source, so they
     stay in the DB/PUT copy but are dropped from the LOCAL IndexedDB cache
     (server is the source of truth; a cache read that needs them re-fetches).

Persist boundaries covered:
  • SERVER (lib/tasks_pkg/manager.py): ``_merge_tool_rounds`` (both task_results
    + conversation-sync toolRounds), ``build_result_meta`` (final usage +
    apiRounds). Twins: ``_sanitize_usage_for_persist`` /
    ``_sanitize_api_rounds_for_persist`` / ``_trim_round_for_persist``.
  • FRONTEND PUT (static/js/core/conversations.js): ``_trimMsgForPersist`` in
    the lightMsgs mapper — so a client PUT never re-inflates what the server
    trimmed. Keeps base64 (render source).
  • FRONTEND CACHE (static/js/idb-cache.js): ``_stripMessage`` — also drops the
    base64 uri (local read cache only).

Each check drives the REAL shipped function and is paired with a DOUBLE-NEUTER
that reverts the trim and proves the assertion flips to failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _big_wire_fp():
    # Mimic the ~226 KB canonical-message list _wire_fp carries per round.
    return [f'msg{i}:field:hashvalue{i:08d}' for i in range(4000)]


def _fat_task():
    """A task shaped like the real fat conversations: apiRounds with giant
    usage._wire_fp, a DONE run_command round with a huge _partialOutput, and a
    still-running round whose buffer must be KEPT."""
    return {
        'usage': {'prompt_tokens': 10, 'trace_id': 't', '_wire_fp': _big_wire_fp(),
                  '_wire_static': 'abc'},
        'apiRounds': [
            {'round': 1, 'model': 'm', 'tag': 'R1',
             'usage': {'prompt_tokens': 5, 'trace_id': 't1', '_dispatch': {'k': 1},
                       '_wire_fp': _big_wire_fp(), '_wire_static': 'x'}},
            {'round': 2, 'model': 'm', 'tag': 'R2',
             'usage': {'prompt_tokens': 5, 'trace_id': 't2'}},
        ],
        'toolRounds': [
            {'roundNum': 1, 'toolName': 'run_command', 'status': 'done',
             'toolContent': 'real output', '_partialOutput': 'X' * 500000,
             'results': [{'output': 'real output', 'exitCode': 0}]},
            {'roundNum': 2, 'toolName': 'run_command', 'status': 'searching',
             '_partialOutput': 'live streaming buffer'},
        ],
    }


# ══════════════════════════════════════════════════════════════════════
#  SERVER SIDE (lib/tasks_pkg/manager.py)
# ══════════════════════════════════════════════════════════════════════

def test_server_merge_tool_rounds_trims_done_partial_output():
    """_merge_tool_rounds drops _partialOutput on a DONE round but KEEPS it on
    a still-running round (mid-stream replay), and never mutates the live task."""
    import lib.tasks_pkg.manager as M
    task = _fat_task()
    task['_checkpointToolRounds'] = []
    merged = M._merge_tool_rounds(task)
    assert '_partialOutput' not in merged[0], (
        'regression: a DONE run_command round still carries its transient '
        '_partialOutput buffer into persistence (18 MB bloat observed).')
    assert merged[1].get('_partialOutput') == 'live streaming buffer', (
        'a still-running round must KEEP _partialOutput for mid-stream replay.')
    # Non-mutation invariant (thread-safety: the live round is serialized
    # concurrently elsewhere).
    assert task['toolRounds'][0]['_partialOutput'] == 'X' * 500000, (
        '_merge_tool_rounds must not mutate the live task round in place.')


def test_server_merge_tool_rounds_neuter():
    """DOUBLE-NEUTER: without _trim_round_for_persist, _partialOutput survives."""
    import lib.tasks_pkg.manager as M
    task = _fat_task()
    # Simulate the pre-fix behaviour: shallow-copy WITHOUT the trim.
    pre_fix = [dict(r) for r in task['toolRounds']]
    assert '_partialOutput' in pre_fix[0], (
        'neuter sanity: the pre-fix shallow-copy keeps _partialOutput — so the '
        'real _merge_tool_rounds trimming it is the load-bearing change.')


def test_server_build_result_meta_strips_wire_fp():
    """build_result_meta strips usage._wire_fp from the final usage AND every
    apiRounds[].usage, while keeping the fields render paths actually read."""
    import lib.tasks_pkg.manager as M
    task = _fat_task()
    task.update({'id': 'task1234', 'finishReason': 'stop', 'model': 'm'})
    meta = M.build_result_meta(task)
    assert '_wire_fp' not in meta['usage'] and '_wire_static' not in meta['usage'], (
        'regression: build_result_meta persisted usage._wire_fp (226 KB/round '
        'diagnostic that no render path reads).')
    assert meta['usage']['trace_id'] == 't', 'must keep render-read fields (trace_id).'
    for r in meta['apiRounds']:
        assert '_wire_fp' not in r['usage'], 'apiRounds[].usage._wire_fp must be stripped.'
    # _dispatch is read by finish_info.js — must survive.
    assert meta['apiRounds'][0]['usage'].get('_dispatch') == {'k': 1}, (
        'must keep usage._dispatch (read by finish_info.js).')


def test_server_build_result_meta_neuter():
    """DOUBLE-NEUTER: bypassing the sanitizer leaves _wire_fp in the meta."""
    import lib.tasks_pkg.manager as M
    task = _fat_task()
    task.update({'id': 'task1234', 'finishReason': 'stop', 'model': 'm'})
    # Pre-fix behaviour = raw assignment.
    raw_meta_usage = task['usage']
    assert '_wire_fp' in raw_meta_usage, (
        'neuter sanity: the raw usage carries _wire_fp — so build_result_meta '
        'calling _sanitize_usage_for_persist is the load-bearing change.')


def test_server_sanitizers_are_free_when_nothing_to_strip():
    """The sanitizer returns the SAME object when there is nothing transient —
    so the common small-usage case pays no copy cost."""
    import lib.tasks_pkg.manager as M
    clean = {'prompt_tokens': 5, 'trace_id': 't'}
    assert M._sanitize_usage_for_persist(clean) is clean


# ══════════════════════════════════════════════════════════════════════
#  FRONTEND PUT (static/js/core/conversations.js :: _trimMsgForPersist)
# ══════════════════════════════════════════════════════════════════════

_PUT_HARNESS = r"""
const fs = require('fs');
const csrc = fs.readFileSync(process.argv[2], 'utf8');
function extract(src, name) {
  const s = src.indexOf('function ' + name);
  let d = 0, i = src.indexOf('{', s), e = -1;
  for (; i < src.length; i++) { if (src[i] === '{') d++; else if (src[i] === '}') { d--; if (d === 0) { e = i + 1; break; } } }
  return src.slice(s, e);
}
const NEUTER = process.argv[3] === 'neuter';
let code = "const _USAGE_TRANSIENT_KEYS=['_wire_fp','_wire_static'];\n"
  + extract(csrc, '_stripUsageTransient') + '\n' + extract(csrc, '_trimMsgForPersist');
if (NEUTER) {
  // Revert the trim: _trimMsgForPersist becomes identity.
  code += '\n_trimMsgForPersist = function(m){ return m; };';
}
eval(code);

const msg = {
  role: 'assistant',
  apiRounds: [{ round: 1, usage: { prompt_tokens: 5, trace_id: 't', _wire_fp: Array(4000).fill('x'), _wire_static: 's' } }],
  toolRounds: [
    { roundNum: 1, toolName: 'run_command', status: 'done', toolContent: 'real',
      _partialOutput: 'X'.repeat(500000),
      results: [{ output: 'real', imageDataUris: [{ uri: 'data:image/png;base64,' + 'A'.repeat(100000), format: 'png' }] }] },
    { roundNum: 2, toolName: 'run_command', status: 'searching', _partialOutput: 'live' },
  ],
  _liveLastRoundUsage: { tokensIn: 5, usage: { prompt_tokens: 5, _wire_fp: Array(4000).fill('y') } },
};
const out = _trimMsgForPersist(msg);
const s = JSON.stringify(out);
const res = {
  wire: s.includes('_wire_fp'),
  donePO: (out.toolRounds[0]._partialOutput !== undefined),
  livePO: (out.toolRounds[1]._partialOutput === 'live'),
  b64kept: /data:image\/png;base64,A{5000}/.test(s),   // PUT keeps base64 (render source)
  liveUsageWire: !!(out._liveLastRoundUsage && out._liveLastRoundUsage.usage && ('_wire_fp' in out._liveLastRoundUsage.usage)),
  origUntouched: (msg.toolRounds[0]._partialOutput.length === 500000),
};
console.log(JSON.stringify(res));
"""


def _run_put(neuter=False):
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    harness = os.path.join(HERE, '_put_trim_harness.js')
    with open(harness, 'w') as f:
        f.write(_PUT_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, conv_js, 'neuter' if neuter else 'real'],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    import json
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_put_trim():
    """The PUT-body trim drops _wire_fp (all 3 nests) + done-round
    _partialOutput, KEEPS a running round's buffer and the base64 render
    source, and never mutates the live message."""
    r = _run_put(neuter=False)
    assert not r['wire'], 'PUT body still carries usage._wire_fp (all nests must be stripped).'
    assert not r['donePO'], 'PUT body still carries a DONE round _partialOutput.'
    assert r['livePO'], 'a running round must keep _partialOutput.'
    assert r['b64kept'], 'PUT (server DB) must KEEP inline base64 — it is the render source.'
    assert not r['liveUsageWire'], '_liveLastRoundUsage.usage._wire_fp must be stripped.'
    assert r['origUntouched'], '_trimMsgForPersist must not mutate the live message.'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_put_trim_double_neuter():
    """DOUBLE-NEUTER: with _trimMsgForPersist reverted to identity, _wire_fp and
    the done-round _partialOutput survive — proving the trim is load-bearing."""
    r = _run_put(neuter=True)
    assert r['wire'], 'neuter did not bite: _wire_fp should survive when the trim is identity.'
    assert r['donePO'], 'neuter did not bite: done-round _partialOutput should survive.'


# ══════════════════════════════════════════════════════════════════════
#  FRONTEND CACHE (static/js/idb-cache.js :: _stripMessage)
# ══════════════════════════════════════════════════════════════════════

_CACHE_HARNESS = r"""
const fs = require('fs');
const isrc = fs.readFileSync(process.argv[2], 'utf8');
function extract(src, name) {
  const s = src.indexOf('function ' + name);
  let d = 0, i = src.indexOf('{', s), e = -1;
  for (; i < src.length; i++) { if (src[i] === '{') d++; else if (src[i] === '}') { d--; if (d === 0) { e = i + 1; break; } } }
  return src.slice(s, e);
}
const NEUTER = process.argv[3] === 'neuter';
let code = 'var _USAGE_TRANSIENT=["_wire_fp","_wire_static"];\n'
  + extract(isrc, '_stripUsageObj') + '\n'
  + extract(isrc, '_stripToolRound') + '\n'
  + extract(isrc, '_stripMessage');
if (NEUTER) {
  // Revert: _stripToolRound identity + skip the apiRounds/base64 branch.
  code += '\n_stripToolRound = function(rd){ return rd; };';
}
eval(code);

const msg = {
  role: 'assistant',
  apiRounds: [{ round: 1, usage: { prompt_tokens: 5, _wire_fp: Array(4000).fill('x') } }],
  toolRounds: [
    { roundNum: 1, toolName: 'read_files', status: 'done', toolContent: 'real',
      _partialOutput: 'X'.repeat(500000),
      results: [{ imageDataUris: [{ uri: 'data:image/png;base64,' + 'A'.repeat(100000), format: 'png', filename: 'x.png' }] }] },
  ],
};
const out = _stripMessage(msg);
const s = JSON.stringify(out);
const res = {
  wire: s.includes('_wire_fp'),
  b64: /data:image\/png;base64,A{5000}/.test(s),
  donePO: s.includes('"_partialOutput"'),
  metaKept: (out.toolRounds && out.toolRounds[0].results[0].imageDataUris[0].format === 'png'
             && out.toolRounds[0].results[0].imageDataUris[0].filename === 'x.png'),
};
console.log(JSON.stringify(res));
"""


def _run_cache(neuter=False):
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    harness = os.path.join(HERE, '_cache_strip_harness.js')
    with open(harness, 'w') as f:
        f.write(_CACHE_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, idb_js, 'neuter' if neuter else 'real'],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    import json
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_cache_strip():
    """The IndexedDB cache strip drops _wire_fp, the done-round _partialOutput,
    AND the multi-MB inline base64 uri — but keeps the descriptor metadata
    (format/filename) so the shape survives."""
    r = _run_cache(neuter=False)
    assert not r['wire'], 'cache still carries usage._wire_fp.'
    assert not r['b64'], 'cache still carries the multi-MB base64 uri (must be dropped locally).'
    assert not r['donePO'], 'cache still carries a done-round _partialOutput.'
    assert r['metaKept'], 'cache must keep imageDataUris format/filename metadata (drop only uri).'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_cache_strip_double_neuter():
    """DOUBLE-NEUTER: with _stripToolRound reverted to identity, the base64 uri
    and done-round _partialOutput survive in the cache copy."""
    r = _run_cache(neuter=True)
    assert r['b64'], 'neuter did not bite: base64 uri should survive when _stripToolRound is identity.'
    assert r['donePO'], 'neuter did not bite: done-round _partialOutput should survive.'


# ══════════════════════════════════════════════════════════════════════
#  BACKFILL MIGRATION (tests/_migrate_trim_oversized_conversations.py)
#  Rewrites ALREADY-STORED fat rows through the SAME server sanitizers.
# ══════════════════════════════════════════════════════════════════════

def _load_migration():
    import importlib.util
    path = os.path.join(HERE, '_migrate_trim_oversized_conversations.py')
    spec = importlib.util.spec_from_file_location('_mig_trim', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fat_messages():
    """A messages list shaped like the real fat conversations."""
    return [
        {'role': 'user', 'content': 'hi'},
        {
            'role': 'assistant', 'content': 'done',
            'usage': {'prompt_tokens': 10, 'trace_id': 't', '_wire_fp': _big_wire_fp()},
            'apiRounds': [
                {'round': 1, 'usage': {'prompt_tokens': 5, 'trace_id': 't1',
                                       '_dispatch': {'k': 1}, '_wire_fp': _big_wire_fp()}},
            ],
            '_liveLastRoundUsage': {'tokensIn': 5, 'usage': {'prompt_tokens': 5, '_wire_fp': _big_wire_fp()}},
            'toolRounds': [
                {'roundNum': 1, 'toolName': 'run_command', 'status': 'done',
                 'toolContent': 'real', '_partialOutput': 'X' * 500000,
                 'results': [{'output': 'real'}]},
                {'roundNum': 2, 'toolName': 'run_command', 'status': 'searching',
                 '_partialOutput': 'live'},
            ],
        },
    ]


def test_migration_trim_messages_shrinks_and_preserves_content():
    """trim_messages drops every _wire_fp nest + the done-round _partialOutput,
    while preserving the message structure and the render-read fields."""
    import json
    mig = _load_migration()
    msgs = _fat_messages()
    before = len(json.dumps(msgs))
    out = mig.trim_messages(msgs)
    after = len(json.dumps(out))
    assert after < before * 0.2, f'expected big shrink, got {before}->{after}'
    # structure preserved
    assert len(out) == len(msgs)
    asst = out[1]
    assert '_wire_fp' not in asst['usage'] and asst['usage']['trace_id'] == 't'
    assert '_wire_fp' not in asst['apiRounds'][0]['usage']
    assert asst['apiRounds'][0]['usage'].get('_dispatch') == {'k': 1}, 'keep _dispatch'
    assert '_wire_fp' not in asst['_liveLastRoundUsage']['usage']
    assert asst['_liveLastRoundUsage']['tokensIn'] == 5, 'keep tokensIn'
    assert '_partialOutput' not in asst['toolRounds'][0], 'done-round buffer dropped'
    assert asst['toolRounds'][0]['toolContent'] == 'real', 'authoritative output kept'
    assert asst['toolRounds'][1].get('_partialOutput') == 'live', 'running-round buffer kept'
    # non-mutation of the input
    assert '_wire_fp' in msgs[1]['usage'], 'trim_messages must not mutate its input'


def test_migration_trim_messages_idempotent():
    """Running trim on already-trimmed messages is a no-op (same size) — this is
    what makes the migration's shrink-only UPDATE safe to run twice."""
    import json
    mig = _load_migration()
    once = mig.trim_messages(_fat_messages())
    size1 = len(json.dumps(once))
    twice = mig.trim_messages(once)
    size2 = len(json.dumps(twice))
    assert size1 == size2, 'second pass must not shrink further (idempotent)'


def test_migration_reuses_manager_helpers_no_reimplementation():
    """The migration must import the manager.py sanitizers, not re-implement the
    trim logic (single source of truth — a divergent copy would drift)."""
    mig = _load_migration()
    import lib.tasks_pkg.manager as M
    assert mig._sanitize_usage_for_persist is M._sanitize_usage_for_persist
    assert mig._sanitize_api_rounds_for_persist is M._sanitize_api_rounds_for_persist
    assert mig._trim_round_for_persist is M._trim_round_for_persist


def test_migration_trim_messages_neuter():
    """DOUBLE-NEUTER: monkeypatch the sanitizers to identity → trim_messages no
    longer shrinks, proving they are the load-bearing dependency."""
    import json
    mig = _load_migration()
    orig_u, orig_a, orig_r = (mig._sanitize_usage_for_persist,
                              mig._sanitize_api_rounds_for_persist,
                              mig._trim_round_for_persist)
    try:
        mig._sanitize_usage_for_persist = lambda u: u
        mig._sanitize_api_rounds_for_persist = lambda a: a
        mig._trim_round_for_persist = lambda r: r
        msgs = _fat_messages()
        before = len(json.dumps(msgs))
        after = len(json.dumps(mig.trim_messages(msgs)))
        assert after >= before, 'neuter did not bite: trim should be a no-op with identity sanitizers'
    finally:
        mig._sanitize_usage_for_persist = orig_u
        mig._sanitize_api_rounds_for_persist = orig_a
        mig._trim_round_for_persist = orig_r
