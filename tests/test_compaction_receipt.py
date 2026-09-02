"""Bounded structured compaction-result contracts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from lib.tasks_pkg.compaction._receipt import build_compaction_receipt


pytestmark = pytest.mark.unit


def test_receipt_is_bounded_finite_and_does_not_duplicate_summary_text():
    summary = 'private continuation body ' * 500
    receipt = build_compaction_receipt(
        trigger='working_set',
        status='completed',
        strategy='selective_summary',
        implementation='model_summary',
        continuation_format='context_compact_tool',
        summary_generated=True,
        summary_text=summary,
        summary_usage={'input_tokens': 1200, 'output_tokens': 300},
        recent_files=[f'lib/file-{index}.py' for index in range(20)],
        economics={
            'dropped_tokens': 10_000,
            'payback_rounds': float('inf'),
            'payback_limit_rounds': 6,
            'payback_policy': 'adaptive_expected_horizon',
            'pricing_source': 'test',
        },
    )

    encoded = json.dumps(receipt, allow_nan=False).encode('utf-8')
    assert receipt['schemaVersion'] == 'tofu.compaction-receipt/v1'
    assert receipt['summary']['usage']['totalTokens'] == 1500
    assert receipt['summary']['chars'] == len(summary)
    assert summary not in encoded.decode('utf-8')
    assert len(receipt['retention']['recentFiles']) == 8
    assert receipt['economics']['paybackRounds'] is None
    assert receipt['economics']['paybackLimitRounds'] == 6
    assert receipt['economics']['paybackPolicy'] == 'adaptive_expected_horizon'
    assert len(encoded) < 32 * 1024


def test_proactive_head_truncate_archives_before_mutation(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction import _reactive as reactive
    from lib.tasks_pkg.compaction import _tokens as tokens
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager

    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'old objective'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'current request'},
        {'role': 'assistant', 'content': 'current answer'},
    ]
    original = [dict(message) for message in messages]
    archived = []
    events = []

    def decline(_messages, task=None, **kwargs):
        kwargs['_result_meta'].update({
            'compacted': False,
            'msgs_before': len(_messages),
            'summary_usage': {'input_tokens': 25, 'output_tokens': 0},
            'summary_duration_ms': 12,
        })
        return 'summary failed'

    def archive(_conv_id, snapshot, **_kwargs):
        archived.append([dict(message) for message in snapshot])
        return 'fallback-archive'

    def truncate(snapshot, _task, **_kwargs):
        del snapshot[1:3]
        return 2

    store = MagicMock()
    monkeypatch.setattr(compact, 'execute_compact_tool', decline)
    monkeypatch.setattr(compact, '_archive_transcript', archive)
    monkeypatch.setattr(compact, '_get_context_limit', lambda _task: 100)
    monkeypatch.setattr(compact, '_usable_context', lambda _limit: 100)
    monkeypatch.setattr(tokens, '_count_tokens_authoritative',
                        lambda _messages, _task: (200, 'mock'))
    monkeypatch.setattr(reactive, '_head_truncate', truncate)
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event',
                        lambda _task, event: events.append(event))

    ok = compact.force_compact_if_needed(
        messages,
        task={'id': 'task', 'convId': 'conversation', '_userId': 1,
              'config': {'model': 'mock'}},
        force=True,
        _allow_head_truncate_fallback=True,
        _compaction_round=3,
    )

    assert ok is True
    assert archived == [original], 'archive must be the exact pre-truncate list'
    receipt = store.update_archive_summary.call_args.kwargs['receipt']
    assert receipt['strategy'] == 'deterministic_recovery'
    assert receipt['recovery']['droppedMessages'] == 2
    assert events[-1]['receipt'] == receipt
