#!/usr/bin/env python3
"""Display-patch frame after a late args repair (the '$ ?' incident).

Incident (2026-08-06, conv msebjymx5b4a25 task e5198ff6 round 109, byte-level
evidence in logs/raw_sse_anomaly.log trace 3da3af106f2f4cf585a7aa9d0ef8315e):
the gateway cut the SSE stream MID-ARGUMENTS — the last raw frame ends
``{"arguments":".py"}``, the closing of the arguments JSON never arrived. The
streaming early-announce fired on the truncated blob; ``json.loads`` failed,
``fn_args={}`` and the display builder produced the literal ``'?'`` — the
frontend rendered ``$ ?``. The post-stream parse then RECOVERED the args via
``repair_json`` and the command executed correctly
(``git status --short lib/llm_dispatch/api.py``), and
``_apply_repair_to_round`` refreshed the server-side round's query — but it
only mutated the in-memory entry and emitted NO event, so a live client kept
showing ``$ ?`` for the command's whole duration.

The fix: when the repair actually CHANGES the round's display query, the
reuse branch in ``parse_tool_calls`` now pushes a ``tool_progress`` patch
frame (query + _repaired) over the live lane. tool_progress never settles a
round, so the spinner cannot flip early.

Pins:
  1. Incident replay — truncated-args announce ('?') → repaired parse → query
     refreshed AND a tool_progress patch frame emitted (round stays
     'searching', the args really did recover).
  2. Silence gate — a clean announce + clean parse (no repair) emits NO patch
     frame; a repair that does NOT change the display also emits none.
  3. NEUTER — if the refresh returns no-change (monkeypatched), no frame is
     emitted even when a repair summary exists: the emission is driven by the
     refresh signal, not by the mere presence of a repair.

Run standalone:
    python3 tests/test_tool_dispatch_repair_patch.py
or via pytest.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit


_TASK_IDS = itertools.count()


@pytest.fixture(autouse=True)
def _cleanup_chat_runtime_tasks():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    before = set(chat_task_runtime.task_ids())
    yield
    for task_id in set(chat_task_runtime.task_ids()) - before:
        chat_task_runtime.discard(task_id)

from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
from lib.tasks_pkg.tool_dispatch._repair import (
    _apply_repair_to_round,
    _build_repair_summary,
)

_TC_ID = 'run_command_23'  # the incident's gateway-minted tool-call id
# Byte-exact prefix of the incident arguments (the stream was cut right after
# `.py`; closing quote + remaining args + brace never arrived).
_TRUNCATED_ARGS = '{"command": "git status --short lib/llm_dispatch/api.py'
_FULL_COMMAND = 'git status --short lib/llm_dispatch/api.py'


def _make_task():
    task = {
        'id': f'task_repair_patch_{next(_TASK_IDS)}',
        'convId': 'convrepairpatch',
        '_userId': 1,
        'kind': 'chat',
        'status': 'running',
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'toolRounds': [],
        'aborted': False,
        '_tool_schema': [
            {'type': 'function', 'function': {'name': 'run_command',
                                              'parameters': {}}},
        ],
        '_tool_result_cache': {},
    }
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    assert chat_task_runtime.adopt(task) is True
    return task


def _early_announce(task, fn_args, tc_args_str):
    """Mirror StreamingToolAccumulator._emit_tool_start: build the round from
    the args available AT ANNOUNCE TIME and register it as announced."""
    tool_round_num, round_entry, _ = _build_tool_round_entry(
        'run_command', fn_args, _TC_ID, tc_args_str,
        tool_round_num=0, project_enabled=True,
        conv_id=task.get('convId') or task.get('id'),
    )
    round_entry['llmRound'] = 0
    task['toolRounds'].append(round_entry)
    return tool_round_num, round_entry


def _parse(task, arguments_str, early):
    assistant_msg = {
        'content': '',
        'tool_calls': [{
            'id': _TC_ID, 'type': 'function',
            'function': {'name': 'run_command', 'arguments': arguments_str},
        }],
    }
    return parse_tool_calls(
        assistant_msg, task, round_num=0, tool_round_num=1,
        project_enabled=True, early_announced=early,
    )


def _patch_frames(task):
    return [e for e in task['events']
            if isinstance(e, dict) and e.get('type') == 'tool_progress'
            and e.get('_repaired')]


class TestRepairDisplayPatch(unittest.TestCase):

    def test_incident_replay_truncated_announce_patches_display_live(self):
        """The byte-level incident: announce rendered '?', the repaired parse
        must refresh the round's query AND push a tool_progress patch frame
        carrying the corrected display — without settling the round."""
        task = _make_task()
        _, round_entry = _early_announce(task, {}, '{}')
        self.assertEqual(round_entry['query'], '?',
                         'precondition: truncated-args announce renders ?')
        early = {_TC_ID: (round_entry['roundNum'], round_entry)}

        parsed, _ = _parse(task, _TRUNCATED_ARGS, early)

        # The args really recovered (this is what executed in the incident).
        _tc, _fn, _id, fn_args, rn, _re, perr = parsed[0]
        self.assertIsNone(perr)
        self.assertEqual(fn_args.get('command'), _FULL_COMMAND)

        # Display refreshed on the live round entry…
        self.assertEqual(round_entry['query'], _FULL_COMMAND)
        self.assertIn('_repaired', round_entry)
        # …without settling the round…
        self.assertEqual(round_entry['status'], 'searching')
        # …and the patch frame went out on the wire.
        frames = _patch_frames(task)
        self.assertEqual(len(frames), 1,
                         f'expected exactly one display-patch frame, got '
                         f'{[e.get("type") for e in task["events"]]}')
        fr = frames[0]
        self.assertEqual(fr['query'], _FULL_COMMAND)
        self.assertEqual(fr['roundNum'], round_entry['roundNum'])
        self.assertEqual(fr['toolCallId'], _TC_ID)
        self.assertTrue(fr['_repaired'].get('label'))

    def test_clean_args_no_patch_frame(self):
        """No repair → no display change → NO patch frame (hot-path silence)."""
        task = _make_task()
        full_args = {'command': _FULL_COMMAND}
        _, round_entry = _early_announce(task, full_args, json.dumps(full_args))
        self.assertEqual(round_entry['query'], _FULL_COMMAND)
        early = {_TC_ID: (round_entry['roundNum'], round_entry)}

        parsed, _ = _parse(task, json.dumps(full_args), early)
        self.assertIsNone(parsed[0][6])
        self.assertEqual(round_entry['query'], _FULL_COMMAND)
        self.assertEqual(_patch_frames(task), [],
                         'a repair-free parse must not emit a patch frame')

    def test_repair_without_display_change_stays_silent(self):
        """_apply_repair_to_round returns None when the rebuilt display is
        identical to the live one — the badge is still stamped, but no patch
        signal escapes (the caller emits nothing)."""
        task = _make_task()
        full_args = {'command': _FULL_COMMAND}
        _, round_entry = _early_announce(task, full_args, json.dumps(full_args))
        summary = _build_repair_summary(False, [('timeout', 'stringified_primitive')])
        self.assertIsNotNone(summary, 'precondition: a schema coercion summary')

        refreshed = _apply_repair_to_round(
            round_entry, 'run_command', {'command': _FULL_COMMAND, 'timeout': 120},
            summary, True, task['convId'])

        self.assertIsNone(refreshed,
                          'an identical rebuilt display must report no-change')
        self.assertEqual(round_entry['query'], _FULL_COMMAND)
        self.assertEqual(round_entry['_repaired'], summary,
                         'the repair badge is still attached to the round')

    def test_neuter_no_refresh_no_frame(self):
        """NEUTER control: when the refresh reports no-change (patched out),
        the reuse branch must emit NOTHING even with a repair summary in hand
        — proving the frame is driven by the refresh signal, not the summary."""
        import lib.tasks_pkg.tool_dispatch._parse as parse_mod

        task = _make_task()
        _, round_entry = _early_announce(task, {}, '{}')
        early = {_TC_ID: (round_entry['roundNum'], round_entry)}

        original = parse_mod._apply_repair_to_round
        parse_mod._apply_repair_to_round = lambda *a, **k: None
        try:
            parsed, _ = _parse(task, _TRUNCATED_ARGS, early)
        finally:
            parse_mod._apply_repair_to_round = original

        self.assertIsNone(parsed[0][6])
        self.assertEqual(round_entry['query'], '?',
                         'neutered refresh leaves the garbled display in place')
        self.assertEqual(_patch_frames(task), [],
                         'no refresh signal → no patch frame')


if __name__ == '__main__':
    unittest.main(verbosity=2)
