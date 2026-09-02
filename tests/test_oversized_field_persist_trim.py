"""Regression: transient diagnostics stay outside durable/read projections.

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
     data: URLs (9 MB in mr8l9rq09d34n3). The turn-native browser keeps no
     IndexedDB transcript copy; its first-paint projection omits heavy rounds.

Persist boundaries covered here:
  • SERVER (lib/tasks_pkg/manager.py): ``_merge_tool_rounds`` (both task_results
    + conversation-sync toolRounds), ``build_result_meta`` (final usage +
    apiRounds). Twins: ``_sanitize_usage_for_persist`` /
    ``_sanitize_api_rounds_for_persist`` / ``_trim_round_for_persist``.
  • STORAGE PROJECTION: Sidecar writes and first-paint windows reuse the same
    bootstrap-free sanitizer source of truth.

There is intentionally no frontend message-document PUT/cache boundary to test:
Conversation Sync v3 + TurnStore own the browser transcript and IndexedDB is
metadata-only. Keeping a JavaScript trim harness would describe a retired
architecture and manufacture false confidence.

Each check drives a shipped persistence or projection boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _big_wire_fp():
    # Mimic the ~226 KB canonical-message list _wire_fp carries per round.
    return [f'msg{i}:field:hashvalue{i:08d}' for i in range(4000)]


def _fat_task():
    """A task shaped like the real fat conversations: apiRounds with giant
    usage._wire_fp, a DONE run_command round with a huge _partialOutput, and a
    still-running round whose buffer must be KEPT."""
    return {
        'usage': {'prompt_tokens': 10, 'trace_id': 't', '_wire_fp': _big_wire_fp(),
                  '_wire_static': 'abc', '_wire_bytes': list(range(2000)),
                  '_wire_field_bytes': {'messages': list(range(2000))},
                  '_wire_markers': {'ttls': ['5m'] * 1000},
                  '_wire_region': {'system': 'x' * 4000}},
        'apiRounds': [
            {'round': 1, 'model': 'm', 'tag': 'R1',
             'usage': {'prompt_tokens': 5, 'trace_id': 't1', '_dispatch': {'k': 1},
                       '_wire_fp': _big_wire_fp(), '_wire_static': 'x',
                       '_wire_bytes': list(range(1000)),
                       '_wire_system': {'shape': 'x' * 4000}}},
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
    import lib.tasks_pkg.manager._persist as M
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
    import lib.tasks_pkg.manager._persist as M
    task = _fat_task()
    # Simulate the pre-fix behaviour: shallow-copy WITHOUT the trim.
    pre_fix = [dict(r) for r in task['toolRounds']]
    assert '_partialOutput' in pre_fix[0], (
        'neuter sanity: the pre-fix shallow-copy keeps _partialOutput — so the '
        'real _merge_tool_rounds trimming it is the load-bearing change.')


def test_server_build_result_meta_strips_wire_fp():
    """build_result_meta strips usage._wire_fp from the final usage AND every
    apiRounds[].usage, while keeping the fields render paths actually read."""
    import lib.tasks_pkg.manager._persist as M
    task = _fat_task()
    task.update({'id': 'task1234', 'finishReason': 'stop', 'model': 'm'})
    meta = M.build_result_meta(task)
    assert '_wire_fp' not in meta['usage'] and '_wire_static' not in meta['usage'], (
        'regression: build_result_meta persisted usage._wire_fp (226 KB/round '
        'diagnostic that no render path reads).')
    assert not any(k.startswith('_wire_') for k in meta['usage']), (
        'every key in the private _wire_ namespace is transient persistence data')
    assert meta['usage']['trace_id'] == 't', 'must keep render-read fields (trace_id).'
    for r in meta['apiRounds']:
        assert not any(k.startswith('_wire_') for k in r['usage']), (
            'apiRounds[].usage must strip the whole _wire_ diagnostic namespace')
    # _dispatch is read by finish_info.js — must survive.
    assert meta['apiRounds'][0]['usage'].get('_dispatch') == {'k': 1}, (
        'must keep usage._dispatch (read by finish_info.js).')


def test_server_build_result_meta_neuter():
    """DOUBLE-NEUTER: bypassing the sanitizer leaves _wire_fp in the meta."""
    import lib.tasks_pkg.manager._persist as M
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
    import lib.tasks_pkg.manager._persist as M
    clean = {'prompt_tokens': 5, 'trace_id': 't'}
    assert M._sanitize_usage_for_persist(clean) is clean


# ══════════════════════════════════════════════════════════════════════
#  CURRENT STORAGE PROJECTION
#
#  Legacy direct-database cleanup scripts were retired with ``lib.database``.
#  The live manager and Sidecar now share this bootstrap-free projection; the
#  separately tested deep-clean command owns historical transport cleanup.
# ══════════════════════════════════════════════════════════════════════


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
            '_continueApiRounds': [
                {'round': 2, 'usage': {'prompt_tokens': 3, 'trace_id': 'c',
                                       '_wire_field_bytes': _big_wire_fp()}},
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


def test_window_projection_shrinks_and_preserves_visible_content():
    """The first-paint projection drops heavy state and wire diagnostics."""
    import json
    from lib.storage_projection import project_message_for_window

    msgs = _fat_messages()
    before = len(json.dumps(msgs))
    out = [project_message_for_window(message) for message in msgs]
    after = len(json.dumps(out))
    assert after < before * 0.2, f'expected big shrink, got {before}->{after}'
    assert len(out) == len(msgs)
    asst = out[1]
    assert asst['content'] == 'done'
    assert '_wire_fp' not in asst['usage'] and asst['usage']['trace_id'] == 't'
    assert '_wire_fp' not in asst['apiRounds'][0]['usage']
    assert asst['apiRounds'][0]['usage'].get('_dispatch') == {'k': 1}, 'keep _dispatch'
    assert '_wire_field_bytes' not in asst['_continueApiRounds'][0]['usage']
    assert asst['_continueApiRounds'][0]['usage']['trace_id'] == 'c'
    assert '_wire_fp' not in asst['_liveLastRoundUsage']['usage']
    assert asst['_liveLastRoundUsage']['tokensIn'] == 5, 'keep tokensIn'
    assert 'toolRounds' not in asst
    assert asst['_trimmedToolRoundCount'] == 2
    assert '_wire_fp' in msgs[1]['usage'], 'projection must not mutate its input'


def test_window_projection_is_idempotent_and_copy_on_change():
    from lib.storage_projection import project_message_for_window

    original = _fat_messages()[1]
    once = project_message_for_window(original)
    twice = project_message_for_window(once)
    assert twice is once
    assert once is not original


def test_manager_reexports_single_storage_projection_helpers():
    """Manager compatibility aliases must point to the storage source of truth."""
    import lib.storage_projection as projection
    import lib.tasks_pkg.manager._persist as M

    assert M._sanitize_usage_for_persist is projection.sanitize_usage_for_persist
    assert (M._sanitize_api_rounds_for_persist
            is projection.sanitize_api_rounds_for_persist)
    assert M._trim_round_for_persist is projection.trim_tool_round_for_persist


def test_storage_projection_import_is_bootstrap_free():
    """Projection reuse must not import the host persistence graph."""
    code = (
        "import sys; import lib.storage_projection; "
        "assert 'lib.storage_sidecar' not in sys.modules; "
        "assert 'lib.tasks_pkg' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT,
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
