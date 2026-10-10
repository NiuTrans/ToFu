"""Shared child compaction uses its own ledger without touching parent state."""
from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lib.tasks_pkg.invocation import InvocationPolicy
from lib.tasks_pkg.compaction._ownership import compaction_owner_key

pytestmark = pytest.mark.unit


@pytest.fixture
def family():
    namespace = 'compaction-ownership-' + uuid4().hex
    root = {'id': namespace + '-root', 'convId': namespace, '_userId': 91827,
            'config': {}}
    children = [{**root, 'id': namespace + suffix,
                 '_invocationPolicy': InvocationPolicy.child(root['id'])}
                for suffix in ('-a', '-b')]
    yield root, *children
    from lib.tasks_pkg.compaction._compaction_usage import reset_compaction_usage
    from lib.tasks_pkg.compaction._constants import _cooldown_lock, _summary_cooldowns
    for task in (root, *children):
        key = compaction_owner_key(task)
        reset_compaction_usage(key)
        with _cooldown_lock:
            _summary_cooldowns.pop(key, None)


def test_key_root_compatibility_and_child_fail_closed(family):
    root, a, b = family
    assert compaction_owner_key(root) == root['convId']
    assert compaction_owner_key(root, 'explicit') == 'explicit'
    assert compaction_owner_key(None, 'direct') == 'direct'
    assert compaction_owner_key(None) == ''
    assert compaction_owner_key(a, root['convId']) == compaction_owner_key(a)
    assert len({compaction_owner_key(t) for t in family}) == 3
    with pytest.raises(ValueError):
        compaction_owner_key({**a, 'id': ''})
    assert a['convId'] == root['convId'] == b['convId']


def test_summary_affinity_isolates_siblings_even_with_parent_argument(family):
    from lib.tasks_pkg.compaction._layer2._summary import _summary_cache_affinity_id
    root, a, b = family
    identities = [_summary_cache_affinity_id(root['convId'], task) for task in family]
    assert len(set(identities)) == 3
    assert identities[1] == _summary_cache_affinity_id('', a)


def test_real_summary_and_advanced_calls_share_child_usage_only(monkeypatch, family):
    import lib.llm_dispatch as dispatch
    import lib.tasks_pkg.compaction._layer2._summary as summary
    from lib.tasks_pkg.compaction._advanced import _make_summarize_fn
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage, pop_compaction_usage)
    root, child, sibling = family
    child['config'] = {'model': 'fixture-model'}
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._summary_routing.select_summary_route',
        lambda *a, **kw: {
            'policy': 'main', 'modelId': 'fixture-model',
            'providerId': 'fixture-provider',
        })
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(dispatch, 'dispatch_chat', lambda *a, **kw: (
        'Preserved child result.', {'prompt_tokens': 20, 'completion_tokens': 3,
                                    'cached_tokens': 5, 'finish_reason': 'stop'}))

    from lib.llm.stream_result import ProviderStreamResult

    def stream_summary(*args, **kwargs):
        assert kwargs['prefer_model'] == 'fixture-model'
        assert kwargs['strict_model'] is True
        assert callable(kwargs['abort_check'])
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Preserved child result.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 3,
                     'cached_tokens': 5})

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream_summary)
    record_compaction_usage(root['convId'], {'prompt_tokens': 101})
    parent_before = deepcopy(get_compaction_usage(root['convId']))
    assert summary._generate_query_aware_summary(
        [{'role': 'user', 'content': 'child goal'}], 'child goal',
        conv_id=root['convId'], task=child)
    assert _make_summarize_fn(root['convId'], child)('child facts')
    usage = pop_compaction_usage(compaction_owner_key(child))
    assert usage['n_calls'] == 2
    assert usage['prompt_tokens'] == 40
    assert usage['cache_read_tokens'] == 10
    assert {row['kind'] for row in usage['calls']} == {'L2', 'advanced'}
    assert get_compaction_usage(root['convId']) == parent_before
    assert get_compaction_usage(compaction_owner_key(sibling)) == {}
    assert pop_compaction_usage(compaction_owner_key(child)) == {}


def test_archive_create_prune_and_prior_receipt_use_same_scope(monkeypatch, family):
    import lib.agent_core.store as stores
    from lib.tasks_pkg.compaction._archive import _archive_transcript
    from lib.tasks_pkg.compaction._layer2._summary import _archived_prior_receipt
    root, child, _ = family
    store = MagicMock()
    store.archive_transcript.return_value = 'child-archive'
    store.list_compaction_archives.return_value = []
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    assert _archive_transcript(root['convId'], [], task=child,
                               user_id=child['_userId'], emit_event=False) == 'child-archive'
    assert _archived_prior_receipt(child) == ''
    key = compaction_owner_key(child)
    assert store.archive_transcript.call_args.args[0] == key
    assert store.prune_archives.call_args.args[0] == key
    assert store.list_compaction_archives.call_args.args[0] == key
    assert store.archive_transcript.call_args.kwargs['task_id'] == child['id']
    assert store.archive_transcript.call_args.kwargs['receipt']['status'] == 'pending'


def test_scoped_archive_retention_bounds_the_created_scope_only(monkeypatch, family):
    import lib.agent_core.store as stores
    from lib.tasks_pkg.compaction import _constants
    from lib.tasks_pkg.compaction._archive import _archive_transcript

    root, child, sibling = family
    child['_archiveScope'] = 'invocation:owned-child'
    sibling_scope = 'invocation:owned-sibling'
    buckets = {'': ['parent'], sibling_scope: ['sibling'], child['_archiveScope']: []}
    sequence = []

    class ScopedStore:
        def archive_transcript(self, conv_id, messages, *, user_id, archive_scope, **kwargs):
            assert conv_id == root['convId'] and user_id == child['_userId']
            assert archive_scope == child['_archiveScope']
            sequence.append('create')
            archive_id = str(len(sequence))
            buckets[archive_scope].append(archive_id)
            return archive_id

        def prune_archives(self, conv_id, keep, *, user_id, archive_scope):
            assert conv_id == root['convId'] and user_id == child['_userId']
            assert archive_scope == child['_archiveScope'] and keep == 2
            sequence.append('prune')
            del buckets[archive_scope][:-keep]

    store = ScopedStore()
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(_constants, 'archive_retention', lambda: 2)
    inserted = [_archive_transcript(root['convId'], [], task=child,
                 user_id=child['_userId'], emit_event=False) for _ in range(5)]
    assert buckets[child['_archiveScope']] == inserted[-2:]
    assert buckets[''] == ['parent'] and buckets[sibling_scope] == ['sibling']
    assert sequence == ['create', 'prune'] * 5


def test_archived_prior_receipt_uses_authorized_child_scope_without_parent_fallback(monkeypatch, family):
    import lib.agent_core.store as stores
    from lib.tasks_pkg.compaction._layer2._summary import _archived_prior_receipt
    root, child, _ = family
    child['_archiveScope'] = 'invocation:owned-child'
    calls = []

    class ScopedStore:
        def list_compaction_archives(self, conv_id, *, user_id, limit, archive_scope):
            assert conv_id == root['convId'] and user_id == child['_userId']
            assert archive_scope == child['_archiveScope'] and limit == 8
            calls.append('list')
            return [{'id': 'child-receipt'}]

        def get_compaction_archive(self, conv_id, archive_id, *, user_id,
                                   include_messages, archive_scope):
            assert conv_id == root['convId'] and user_id == child['_userId']
            assert archive_id == 'child-receipt' and not include_messages
            assert archive_scope == child['_archiveScope']
            calls.append('get')
            return {'archive': {'summary': '### Objective\nScoped child state'}}

    monkeypatch.setattr(stores, 'get_conversation_store', ScopedStore)
    assert _archived_prior_receipt(child) == '### Objective\nScoped child state'
    assert calls == ['list', 'get']
    assert _archived_prior_receipt({**child, '_userId': True}) == ''
    assert calls == ['list', 'get']


def test_l1_cache_read_uses_child_key(monkeypatch, family):
    import lib.tasks_pkg.cache_tracking._prefix as prefix
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    root, child, _ = family
    seen = []
    monkeypatch.setattr(prefix, 'get_cache_prefix_count',
                        lambda key, **kw: seen.append(key) or 0)
    micro_compact([{'role': 'user', 'content': 'goal'}],
                  conv_id=root['convId'], task=child)
    assert seen == [compaction_owner_key(child)]


def test_parent_cooldown_does_not_suppress_child(monkeypatch, family):
    import time
    import lib.tasks_pkg.compaction._tokens as tokens
    from lib.tasks_pkg.compaction._constants import _cooldown_lock, _summary_cooldowns
    root, child, _ = family
    with _cooldown_lock:
        _summary_cooldowns[root['convId']] = time.time()
    monkeypatch.setattr(tokens, '_get_context_limit', lambda task: 1000)
    monkeypatch.setattr(tokens, '_count_tokens_authoritative', lambda *a, **kw: (999999, 'test'))
    assert not tokens._should_force_compact([], root)
    assert tokens._should_force_compact([], child)


def test_child_settlement_merges_paid_calls_once_without_parent_drain(family):
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage)
    from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
    root, child, sibling = family
    for task in family:
        record_compaction_usage(compaction_owner_key(task),
                                {'prompt_tokens': 20, 'completion_tokens': 3})
    child['usage'] = {'prompt_tokens': 7, 'completion_tokens': 2}
    accumulated, rounds = {}, []
    settle_compaction_usage(child, accumulated_usage=accumulated,
                            api_rounds=rounds, model='isolated-model')
    assert child['usage']['prompt_tokens'] == 27
    assert child['usage']['completion_tokens'] == 5
    assert len(rounds) == 1 and rounds[0]['kind'] == 'compaction'
    settle_compaction_usage(child, accumulated_usage=accumulated,
                            api_rounds=rounds)
    assert child['usage']['prompt_tokens'] == 27
    assert len(rounds) == 1
    assert get_compaction_usage(compaction_owner_key(child)) == {}
    assert get_compaction_usage(compaction_owner_key(root))['n_calls'] == 1
    assert get_compaction_usage(compaction_owner_key(sibling))['n_calls'] == 1


@pytest.mark.parametrize('cancelled', [False, True])
def test_terminal_error_handlers_drain_child_usage(monkeypatch, family, cancelled):
    import asyncio
    from lib.tasks_pkg.orchestrator import _post_loop
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage)
    root, child, _ = family
    record_compaction_usage(compaction_owner_key(child), {'prompt_tokens': 23})
    record_compaction_usage(compaction_owner_key(root), {'prompt_tokens': 99})
    child['usage'] = {'prompt_tokens': 4}
    observed = []
    monkeypatch.setattr(_post_loop, 'append_event',
                        lambda task, event: observed.append(task['usage'].copy()))
    import lib.tasks_pkg.manager as manager
    monkeypatch.setattr(manager, 'stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr(_post_loop, 'persist_task_result',
                        lambda task: pytest.fail('child persisted as root'))
    if cancelled:
        error = asyncio.CancelledError('isolated cancellation')
        with pytest.raises(asyncio.CancelledError) as raised:
            _post_loop.handle_task_base_exception(child, error)
        assert raised.value is error
    else:
        assert _post_loop.handle_task_fatal(child, RuntimeError('isolated failure'))
    assert observed[0]['prompt_tokens'] == 27
    assert get_compaction_usage(compaction_owner_key(child)) == {}
    assert get_compaction_usage(compaction_owner_key(root))['prompt_tokens'] == 99
