"""Utility recovery preserves archive order, honest outcomes and dispatch facts."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('dropped', [2, 0], ids=['head-truncate', 'no-safe-message'])
@pytest.mark.parametrize('fault', ['none', 'archive', 'store', 'event'])
@pytest.mark.parametrize('usage,expected_calls', [
    ({'input_tokens': 25, 'output_tokens': 0, '_summaryCalls': [
        {'usage': {'_dispatch': {'model': 'observed-model',
                                'provider_id': 'observed-provider',
                                'api_key': 'must-not-be-emitted'}}},
        {'usage': {'_dispatch': {'model': 'retry-model',
                                'provider_id': 'retry-provider'}}},
        {'usage': {}},
    ]}, [{'model': 'observed-model', 'providerId': 'observed-provider'},
         {'model': 'retry-model', 'providerId': 'retry-provider'}, {}]),
    (None, []),
], ids=['observed-dispatches', 'no-usage'])
def test_fallback_done_retains_summary_dispatch_calls(
        monkeypatch, dropped, fault, usage, expected_calls):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction import _reactive as reactive
    from lib.tasks_pkg.compaction import _tokens
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager

    # An explicit legacy utility has no live task ID. Exercise its actual
    # eligibility without weakening live/transient source preservation.
    task = {'convId': 'isolated-fallback', '_archiveScope': 'utility-scope',
            '_userId': 1, 'config': {'model': 'mock'}}
    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'old objective'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'current request'},
        {'role': 'assistant', 'content': 'current answer'},
    ]
    original = deepcopy(messages)
    archived, events, order = [], [], []
    store = MagicMock()

    def decline(source, task=None, **kwargs):
        kwargs['_result_meta'].update(
            compacted=False, msgs_before=len(source),
            summary_usage=usage, summary_duration_ms=12)
        return 'summary failed'

    def archive(_conv_id, source, **kwargs):
        order.append('archive')
        archived.append(deepcopy(source))
        assert kwargs['user_id'] == 1
        if fault == 'archive':
            raise RuntimeError('injected archive failure')
        return 'fallback-archive'

    def truncate(source, _task, **kwargs):
        order.append('truncate')
        assert kwargs['event_name'] == 'proactive_head_truncate'
        if dropped:
            del source[1:3]
        return dropped

    def update(*args, **kwargs):
        order.append('store')
        if fault == 'store':
            raise RuntimeError('injected receipt persistence failure')

    def emit(task, event):
        order.append('event')
        events.append(event)
        if fault == 'event':
            raise RuntimeError('injected event publication failure')

    monkeypatch.setattr(compact, 'execute_compact_tool', decline)
    monkeypatch.setattr(compact, '_archive_transcript', archive)
    monkeypatch.setattr(compact, '_get_context_limit', lambda task: 100)
    monkeypatch.setattr(compact, '_usable_context', lambda limit: 100)
    monkeypatch.setattr(compact, '_estimate_total_tokens', lambda source: 20 * len(source))
    monkeypatch.setattr(_tokens, '_count_tokens_authoritative',
                        lambda source, task: (200, 'mock'))
    monkeypatch.setattr(reactive, '_head_truncate', truncate)
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event', emit)
    store.update_archive_summary.side_effect = update

    assert compact.force_compact_if_needed(
        messages, task=task, force=True,
        _allow_head_truncate_fallback=True, _compaction_round=3,
    ) is bool(dropped)

    assert archived == [original]
    assert len(messages) == len(original) - dropped
    if not dropped:
        assert messages == original
    if fault == 'archive':
        assert order == ['archive', 'truncate']
        store.update_archive_summary.assert_not_called()
        assert events == []
        return
    assert order == ['archive', 'truncate', 'store', 'event']
    store.update_archive_summary.assert_called_once()
    args, kwargs = store.update_archive_summary.call_args
    assert args == ('fallback-archive', '', 60 if dropped else 200, len(messages))
    assert kwargs['user_id'] == 1
    assert kwargs['archive_scope'] == 'utility-scope'
    if dropped:
        assert kwargs['post_messages'] == messages
        assert kwargs['post_messages'] is not messages
        assert kwargs['post_messages'][0] is not messages[0]
    else:
        assert kwargs.get('post_messages') is None
    receipt = store.update_archive_summary.call_args.kwargs['receipt']
    assert receipt['status'] == ('completed' if dropped else 'failed')
    assert receipt['strategy'] == 'deterministic_recovery'
    if not dropped:
        assert receipt['outcomeReason'] == 'no_safe_messages_to_drop'
    done = [event for event in events if event.get('type') == 'compaction_done']
    assert len(done) == 1
    assert done[0]['archiveId'] == 'fallback-archive'
    assert done[0]['roundNum'] == 3
    assert done[0]['msgsAfter'] == len(messages)
    assert done[0]['tokensBefore'] == 200
    assert done[0]['tokensAfter'] == (60 if dropped else 200)
    assert done[0]['reductionPct'] == (70.0 if dropped else 0.0)
    assert done[0]['receipt'] == receipt
    assert done[0]['summaryDispatchCalls'] == expected_calls
