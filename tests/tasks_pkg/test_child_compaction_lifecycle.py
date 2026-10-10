"""Executable child cache/accounting lifecycle without provider or storage I/O."""
from copy import deepcopy
from uuid import uuid4

import pytest

from lib.tasks_pkg.compaction._ownership import compaction_owner_key
from lib.tasks_pkg.compaction._compaction_usage import (
    get_compaction_usage, record_compaction_usage, reset_compaction_usage,
)
from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
from lib.tasks_pkg.invocation import InvocationPolicy

pytestmark = pytest.mark.unit


@pytest.fixture
def family():
    namespace = 'child-lifecycle-' + uuid4().hex
    root = {'id': namespace, 'convId': namespace, '_userId': 91827, 'config': {}}
    tasks = [root] + [{**root, 'id': namespace + suffix,
                      '_invocationPolicy': InvocationPolicy.child(root['id'])}
                     for suffix in ('-a', '-b')]
    yield tasks
    from lib.token_counter import invalidate
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    for task in tasks:
        key = compaction_owner_key(task)
        reset_compaction_usage(key)
        invalidate(key)
        _summary_cooldowns.pop(key, None)


def seed(family):
    from lib.tasks_pkg.manager._stream import _record_stream_prompt_usage
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    for index, task in enumerate(family):
        key = compaction_owner_key(task)
        record_compaction_usage(key, {'prompt_tokens': 13 + index})
        _summary_cooldowns[key] = 123
        _record_stream_prompt_usage(task, {'messages': [{'role': 'user', 'content': key}]},
                                    {'prompt_tokens': 101 + index}, {},
                                    model='isolated-model', prefix='test')


def test_stream_cache_and_native_invalidation_are_child_owned(family):
    from lib.token_counter.usage_cache import _lookup
    from lib.tasks_pkg.manager._stream import _record_stream_prompt_usage
    seed(family)
    keys = list(map(compaction_owner_key, family))
    assert [_lookup(key).prompt_tokens for key in keys] == [101, 102, 103]
    assert _record_stream_prompt_usage(family[1], {}, {'effective_prompt_tokens': 7}, {},
                                       model='isolated-model', prefix='test') == 7
    assert _lookup(keys[1]) is None
    assert [_lookup(keys[i]).prompt_tokens for i in (0, 2)] == [101, 103]


@pytest.mark.parametrize('terminal', ['success', 'failure', 'cancel'])
def test_terminal_costs_and_cleanup_preserve_parent_and_sibling(monkeypatch, family, terminal):
    import asyncio
    from lib.tasks_pkg.orchestrator import _finalize, _post_loop, _teardown
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    from lib.token_counter.usage_cache import _lookup
    seed(family)
    root, child, sibling = family
    keys = list(map(compaction_owner_key, family))
    before = [deepcopy(get_compaction_usage(keys[i])) for i in (0, 2)]
    child.update(content='child result', aborted=False, usage={'prompt_tokens': 5},
                 toolRounds=[], messages=[], events=[])
    observed = []
    def emit(task, event):
        observed.append((deepcopy(task['usage']), deepcopy(task.get('apiRounds')), event))
    monkeypatch.setattr(_finalize, 'append_event', emit)
    monkeypatch.setattr(_post_loop, 'append_event', emit)
    monkeypatch.setattr(_finalize, 'stamp_chat_task_terminal', lambda *a, **kw: None)
    import lib.tasks_pkg.manager as manager
    monkeypatch.setattr(manager, 'stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr(_post_loop, 'persist_task_result', lambda *a: pytest.fail('root write'))
    if terminal == 'success':
        _finalize._finalize_and_emit_done(
            child, model='isolated-model', preset='', thinking_depth=None, cfg={},
            last_finish_reason='stop', last_usage={}, last_stream_result=None,
            accumulated_usage=child['usage'], api_rounds=[], tool_call_happened=False,
            messages=[], original_messages=[], all_search_results_text=[], max_tokens=10,
            thinking_enabled=False, temperature=None, _loop_exit_reason='stop',
            _abort_detected_phase=None, project_path='', project_enabled=False,
            round_num=1, assistant_msg={'role': 'assistant', 'content': 'child result'})
    elif terminal == 'failure':
        _post_loop.handle_task_fatal(child, RuntimeError('isolated'))
    else:
        with pytest.raises(asyncio.CancelledError):
            _post_loop.handle_task_base_exception(child, asyncio.CancelledError())
    assert observed[0][0]['prompt_tokens'] == 19
    assert observed[0][1][0]['usage']['prompt_tokens'] == 14
    if terminal == 'success':
        assert observed[0][2]['usage']['prompt_tokens'] == 19
    _teardown.finalize_task_lane(child, 'isolated')
    settle_compaction_usage(child)
    assert child['usage']['prompt_tokens'] == 19
    assert len(child['apiRounds']) == 1
    assert get_compaction_usage(keys[1]) == {} and _lookup(keys[1]) is None
    assert keys[1] not in _summary_cooldowns
    assert [get_compaction_usage(keys[i]) for i in (0, 2)] == before
    assert [_lookup(keys[i]).prompt_tokens for i in (0, 2)] == [101, 103]
    assert all(keys[i] in _summary_cooldowns for i in (0, 2))


@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_failed_settlement_keeps_costs_and_does_not_block_cleanup(monkeypatch, family, error_type):
    import lib.tasks_pkg.llm_fallback._usage as projection
    from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    from lib.token_counter.usage_cache import _lookup
    seed(family)
    child = family[1]
    key = compaction_owner_key(child)
    original = projection.project_usage_for_round_record
    def fail(usage):
        raise error_type('isolated projection failure')
    monkeypatch.setattr(projection, 'project_usage_for_round_record', fail)
    finalize_task_lane(child, 'isolated')
    assert get_compaction_usage(key) == {}
    assert child['_pendingCompactionUsage']['prompt_tokens'] == 14
    assert _lookup(key) is None and key not in _summary_cooldowns
    monkeypatch.setattr(projection, 'project_usage_for_round_record', original)
    settle_compaction_usage(child)
    settle_compaction_usage(child)
    assert child['usage']['prompt_tokens'] == 14
    assert '_pendingCompactionUsage' not in child
    assert len(child['apiRounds']) == 1


def test_completed_children_retain_zero_global_scopes(family):
    from lib.tasks_pkg.compaction._compaction_usage import _usage
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    from lib.token_counter.usage_cache import _cache
    from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
    baseline = tuple(map(len, (_usage, _summary_cooldowns, _cache)))
    for index in range(64):
        child = {**family[1], 'id': family[1]['id'] + str(index)}
        seed([child])
        finalize_task_lane(child, 'isolated')
        assert child['usage']['prompt_tokens'] == 13
    assert tuple(map(len, (_usage, _summary_cooldowns, _cache))) == baseline


def test_root_teardown_does_not_release_conversation_compaction_state(family):
    from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
    from lib.token_counter.usage_cache import _lookup
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    seed(family)
    root = family[0]
    key = compaction_owner_key(root)
    finalize_task_lane(root, 'isolated')
    assert get_compaction_usage(key)['prompt_tokens'] == 13
    assert _lookup(key).prompt_tokens == 101
    assert key in _summary_cooldowns


def test_cleanup_interrupt_does_not_skip_remaining_owners(monkeypatch, family):
    import lib.token_counter as counter
    import lib.llm_dispatch.provider_pin as pin
    import lib.llm_dispatch.conv_affinity as affinity
    from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns
    seed(family)
    seen = []
    def interrupt(key):
        raise KeyboardInterrupt('isolated cleanup failure')
    monkeypatch.setattr(counter, 'invalidate', interrupt)
    monkeypatch.setattr(pin, 'clear_pinned_provider', lambda: seen.append('pin'))
    monkeypatch.setattr(affinity, 'clear_conv_affinity', lambda: seen.append('affinity'))
    finalize_task_lane(family[1], 'isolated')
    assert seen == ['pin', 'affinity']
    assert compaction_owner_key(family[1]) not in _summary_cooldowns
    assert family[1]['usage']['prompt_tokens'] == 14
    monkeypatch.undo()
