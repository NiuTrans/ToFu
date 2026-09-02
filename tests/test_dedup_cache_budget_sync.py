"""Dedup cache must hold the BUDGETED (offloaded) tool result, not the raw one.

Root cause (JOURNAL 2026-07-08 — B2 CDN item, 682 KB balloon): the per-task
``_tool_result_cache`` is populated with the PRE-budget content (parallel-phase
writer / streaming prefetch injector). ``budget_tool_result`` then offloads the
oversized result to disk, but only rewrote the local message copy — the cache
entry kept the full ~682 KB string. That entry then (a) serializes into the
persisted ``raw_state`` (state balloon) and (b) is replayed verbatim on a later
dedup hit, re-flooding context with content already spilled to disk.

The fix (tool_dispatch.execute_tool_pipeline post-phase): after Layer-2 clamp,
sync the budgeted string back into ``content[0]`` of the cache entry when it is
shorter, preserving the rest of the tuple (is_search / source / display /
engine_breakdown / vertical).

Run directly (conda env pytest is flaky):

    python3 tests/test_dedup_cache_budget_sync.py
"""

import os
import sys
import threading
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

import lib.tasks_pkg.tool_dispatch._flags as td
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.tool_dispatch.api import (
    execute_tool_pipeline,
    parse_tool_calls,
)
from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

# A budget well below the payload so the offloader definitely fires. web_search
# budget is 30_000; we return ~120 KB.
_FAKE_TOOL = 'bigsearch_probe'
_PAYLOAD = 'X' * 120_000


def _fake_handler(task, tc, name, tc_id, fn_args, rn, round_entry,
                  cfg, project_path, project_enabled, all_tools=None):
    # Mark is_search=True so it takes the same path as web_search results.
    return tc_id, _PAYLOAD, True


def _schema(names):
    return [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
            for n in names]


def _make_task():
    return {
        'id': 'task_dedupsync_x',
        'convId': 'convdedupsync',
        '_userId': 1,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'toolRounds': [],
        'aborted': False,
        '_tool_schema': _schema([_FAKE_TOOL]),
        '_tool_result_cache': {},
    }


def _assistant(tool_calls):
    return {'content': '', 'tool_calls': tool_calls}


def _tc(name, args='{}', tc_id=None):
    return {'id': tc_id or ('call_' + name), 'type': 'function',
            'function': {'name': name, 'arguments': args}}


def _run_pipeline_once():
    """Register the fake idempotent tool, run one pipeline round, return
    (task, cache_entry_content_len, message_content_len)."""
    task = _make_task()
    parsed, _ = parse_tool_calls(
        _assistant([_tc(_FAKE_TOOL, '{"query": "cdn free tier"}')]),
        task, round_num=0, tool_round_num=0, project_enabled=False,
    )
    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=None, messages=messages,
        all_search_results_text=[], round_num=0, model='test-model',
    )
    cache = task['_tool_result_cache']
    key = _make_cache_key(_FAKE_TOOL, {'query': 'cdn free tier'})
    entry = cache.get(key)
    entry_len = len(entry[0]) if entry and isinstance(entry[0], str) else -1
    msg_len = len(messages[0]['content']) if messages else -1
    return task, entry_len, msg_len, entry


class TestDedupCacheBudgetSync(unittest.TestCase):
    def setUp(self):
        tool_registry.register(_FAKE_TOOL, _fake_handler)
        self._saved_idem = td._IDEMPOTENT_TOOLS
        td._IDEMPOTENT_TOOLS = frozenset(set(td._IDEMPOTENT_TOOLS) | {_FAKE_TOOL})

    def tearDown(self):
        td._IDEMPOTENT_TOOLS = self._saved_idem
        tool_registry._exact.pop(_FAKE_TOOL, None)
        tool_registry._metadata.pop(_FAKE_TOOL, None)

    def test_cache_entry_shrinks_to_budgeted_form(self):
        _task, entry_len, msg_len, entry = _run_pipeline_once()
        # Sanity: the offloader actually fired on the message (budgeted well
        # below the raw payload).
        self.assertGreater(len(_PAYLOAD), 30_000)
        self.assertLess(msg_len, len(_PAYLOAD),
                        'precondition: message content should be offloaded')
        # The dedup cache entry must now match the budgeted message, NOT the
        # raw 120 KB payload.
        self.assertEqual(entry_len, msg_len,
                         'cache entry must be synced to the budgeted content')
        self.assertLess(entry_len, len(_PAYLOAD),
                        'cache entry must NOT retain the raw payload')

    def test_cache_entry_preserves_is_search_flag(self):
        # Syncing content[0] must not disturb the rest of the tuple.
        _task, _entry_len, _msg_len, entry = _run_pipeline_once()
        self.assertIsNotNone(entry)
        self.assertTrue(entry[1], 'is_search flag must be preserved after sync')


if __name__ == '__main__':
    unittest.main(verbosity=2)
