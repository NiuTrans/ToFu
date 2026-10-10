"""Recovery floors and final archive projection contracts (no provider I/O)."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from lib.tasks_pkg.compaction import _reactive as reactive
from lib.tasks_pkg.compaction._constants import _COMPACT_TOOL_NAME
from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

pytestmark = pytest.mark.unit


def pair(name, ident):
    return [
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': ident, 'type': 'function',
             'function': {'name': name, 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': ident, 'name': name,
         'content': [{'type': 'text', 'text': 'continuation'}]},
    ]


@pytest.mark.parametrize('budget', [{'byte_target': 1}, {'token_target': 1}])
def test_head_floor_keeps_current_summary_and_real_batch(budget):
    summary = pair(_COMPACT_TOOL_NAME, 'compact_current')
    real = pair('read_files', 'real')
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'developer', 'content': 'developer rules'},
        {'role': 'user', 'content': 'opening'},
        {'role': 'assistant', 'content': 'spoof context_compact summary'},
        *summary, {'role': 'user', 'content': 'latest'}, *real,
        {'role': 'assistant', 'content': 'old removable prose'},
    ]
    assert _head_truncate(messages, **budget) == 2
    assert all(message in messages for message in summary + real)
    assert messages[1]['role'] == 'developer'
    assert any(m.get('content') == 'latest' for m in messages)
    frozen = deepcopy(messages)
    assert _head_truncate(messages, **budget) == 0
    assert messages == frozen  # Irreducible overflow remains for admission.


@pytest.mark.parametrize('adopted,fail_update', [(True, False), (False, False), (True, True)])
def test_utility_final_recovery_snapshot_and_events(monkeypatch, adopted, fail_update):
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager

    accepted_receipt = {
        'status': 'completed', 'strategy': 'selective_summary',
        'summary': {'generated': True, 'accepted': True, 'chars': 8},
        'retention': {'objectiveAnchored': True},
    }
    # This contract exercises the explicit non-live utility reducer. Live
    # tasks cannot reach the post-summary destructive trim; see safety cases
    # below. Child ownership is a live/transient path and is covered there;
    # it must never read accepted metadata back to trim the accepted summary.
    task = {'convId': 'isolated-recovery', '_userId': 1}
    store = MagicMock()

    def read_accepted(conv_id, archive_id, **kwargs):
        # A strict repository never silently falls back to parent archives.
        assert conv_id == task['convId'] and archive_id == 'archive'
        assert kwargs == dict(user_id=1, include_messages=False,
                              include_post_messages=False)
        return {'archive': {'summary': 'accepted', 'receipt': accepted_receipt}}

    store.get_compaction_archive.side_effect = read_accepted
    if fail_update:
        store.update_archive_summary.side_effect = RuntimeError('offline')
    events = []
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **kw: 'archive')
    monkeypatch.setattr(reactive, '_get_context_limit', lambda task: 1000000)
    monkeypatch.setattr(reactive, '_WIRE_BYTE_SOFT_LIMIT', 100)
    monkeypatch.setattr(reactive, '_strip_images_aggressive', lambda *a, **kw: (0, 0))
    monkeypatch.setattr(reactive, '_truncate_largest_message', lambda *a, **kw: (-1, 0))
    monkeypatch.setattr(reactive, 'micro_compact', lambda *a, **kw: None)
    monkeypatch.setattr(reactive, 'force_compact_if_needed', lambda *a, **kw: adopted)

    def trim(messages, *args, **kwargs):
        messages.pop(0)
        return 1

    monkeypatch.setattr(reactive, '_head_truncate', trim)
    messages = [{'role': 'assistant', 'content': 'x' * 200},
                *pair(_COMPACT_TOOL_NAME, 'compact_current')]
    assert reactive.reactive_compact(messages, task=task)
    call = store.update_archive_summary.call_args
    assert call is not None
    assert call.kwargs['archive_scope'] == task.get('_archiveScope', '')
    assert call.args[1] == ('accepted' if adopted else '')
    assert call.args[2] == reactive._estimate_total_tokens(messages)
    assert call.args[3] == len(messages)
    assert call.kwargs['post_messages'] == messages
    snapshot = deepcopy(call.kwargs['post_messages'])
    messages[-1]['content'][0]['text'] = 'later mutation'
    assert call.kwargs['post_messages'] == snapshot
    receipt = call.kwargs['receipt']
    assert receipt['recovery']['droppedMessages'] == 1
    assert receipt['summary']['accepted'] is adopted
    if adopted:
        assert receipt['retention'] == accepted_receipt['retention']
        assert 'recovery' not in accepted_receipt
        store.get_compaction_archive.assert_called_once_with(
            'isolated-recovery', 'archive', user_id=1,
            include_messages=False, include_post_messages=False)
    else:
        store.get_compaction_archive.assert_not_called()
    assert len(events) == (0 if fail_update else 1)
    if events:
        assert events[-1]['receipt'] == receipt
        assert events[-1]['msgsAfter'] == len(snapshot)


def test_irreducible_recovery_does_not_claim_success(monkeypatch):
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager

    store = MagicMock()
    events = []
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **kw: 'archive')
    monkeypatch.setattr(reactive, '_get_context_limit', lambda task: 1000000)
    monkeypatch.setattr(reactive, '_truncate_largest_message', lambda *a, **kw: (-1, 0))
    monkeypatch.setattr(reactive, 'micro_compact', lambda *a, **kw: None)
    summary = MagicMock(return_value=False)
    monkeypatch.setattr(reactive, 'force_compact_if_needed', summary)
    messages = [{'role': 'user', 'content': 'irreducible'}]
    original = deepcopy(messages)
    assert not reactive.reactive_compact(messages, task={
        'id': 'test', 'convId': 'isolated-no-progress', '_userId': 1})
    assert messages == original
    call = store.update_archive_summary.call_args
    summary.assert_called_once()
    assert summary.call_args.kwargs['_compaction_archive_id'] == 'archive'
    assert call.kwargs['post_messages'] is None
    assert call.kwargs['receipt']['status'] == 'failed'
    assert events[-1]['receipt']['status'] == 'failed'


def test_single_message_shrinker_preserves_policy_and_summary():
    from lib.tasks_pkg.compaction._reactive._strip import _truncate_largest_message

    summary = pair(_COMPACT_TOOL_NAME, 'compact_current')
    summary[-1]['content'] = 'summary' * 3000
    protected = [{'role': 'system', 'content': 'policy' * 3000},
                 {'role': 'developer', 'content': 'developer' * 3000}, *summary]
    execution = pair('read_files', 'real')
    execution[-1]['content'] = 'tool data' * 1000
    messages = deepcopy(protected) + execution
    idx, freed = _truncate_largest_message(messages, ceiling_chars=1000)
    assert idx == len(protected) + 1 and freed > 0
    assert messages[:len(protected)] == protected
    assert _truncate_largest_message(messages[:len(protected)], ceiling_chars=1000) == (-1, 0)


@pytest.fixture(params=['live', 'transient'])
def reactive_runtime(request, monkeypatch):
    """Real reactive → L2 → summary path; only provider/storage I/O is fake."""
    from types import SimpleNamespace
    import lib.agent_core.store as agent_store
    import lib.llm_dispatch as dispatch
    import lib.tasks_pkg.autopilot_state as autopilot
    import lib.tasks_pkg.manager as manager
    from lib.tasks_pkg.compaction import _summary_budget, _summary_routing, _tokens
    from lib.tasks_pkg.compaction._layer2 import _compact as l2, _summary

    task = {'id': 'reactive-runtime', 'convId': 'isolated-source', '_userId': 7,
            'aborted': False, 'round_num': 12, 'config': {'model': 'test'}}
    if request.param == 'transient':
        from lib.tasks_pkg.invocation import InvocationPolicy
        task.update(_transientRuntime=True,
                    _invocationPolicy=InvocationPolicy.child('parent'),
                    _archiveScope='invocation:reactive-runtime')
    messages = [
        {'role': 'user', 'content': 'Original constraints remain authoritative.'},
        {'role': 'assistant', 'content': 'UNIQUE_OLD_EVIDENCE ' * 1000},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Preserve this image and tool evidence.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]},
        *pair('read_files', 'real-batch'),
        {'role': 'user', 'content': 'Continue without deleting source on failure.'},
    ]
    messages[-2]['content'][0]['text'] = 'UNIQUE_TOOL_EVIDENCE ' * 1000
    store, events, snapshots = MagicMock(), [], []

    def archive(_owner, source, **kwargs):
        snapshots.append((deepcopy(source), kwargs))
        return 'runtime-archive'

    def forbidden(*args, **kwargs):
        pytest.fail('Live/transient recovery must not run destructive utility fallback')

    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(reactive, '_archive_transcript', archive)
    monkeypatch.setattr(reactive, '_get_context_limit', lambda task: 100_000)
    # Both byte pressure and large individual rows are present. A passing test
    # must bypass reductions, not merely avoid their size threshold.
    monkeypatch.setattr(reactive, '_WIRE_BYTE_SOFT_LIMIT', 100)
    monkeypatch.setattr(reactive, '_SINGLE_RESULT_HARD_CEILING_CHARS', 100)
    for name in ('_strip_images_aggressive', '_truncate_largest_message',
                 'micro_compact', '_head_truncate'):
        monkeypatch.setattr(reactive, name, forbidden)
    monkeypatch.setattr(l2, '_archive_transcript', forbidden)
    monkeypatch.setattr(l2, '_deterministic_recovery_summary', forbidden)
    monkeypatch.setattr(l2, '_get_context_limit', lambda task: 100_000)
    monkeypatch.setattr(l2, '_plan_cross_turn_tail',
                        lambda source, **kwargs: (2, list(source[2:]), []))
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(_tokens, '_count_tokens_authoritative', lambda *a, **k: (100, 'test'))
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)
    monkeypatch.setattr(_summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(_summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(_summary, '_summary_input_char_budget', lambda task: 120_000)
    monkeypatch.setattr(_summary_routing, 'select_summary_route',
                        lambda *a, **k: {'modelId': 'test', 'providerId': '',
                                        'policy': 'same_model', 'reason': 'test'})
    provider = MagicMock()
    monkeypatch.setattr(dispatch, 'dispatch_stream', provider)
    monkeypatch.setattr(dispatch, 'dispatch_chat', forbidden)
    clock = [0.0]
    monkeypatch.setattr(_summary_budget, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    return SimpleNamespace(task=task, messages=messages, store=store,
                           events=events, snapshots=snapshots,
                           provider=provider, clock=clock)


@pytest.mark.parametrize('failure', ['empty', 'incomplete', 'failed', 'cancelled', 'memory'])
def test_runtime_failure_archives_source_without_adopting_or_trimming(reactive_runtime, failure):
    from lib.llm.stream_result import ProviderStreamResult

    case = reactive_runtime
    original = deepcopy(case.messages)
    identities = [id(message) for message in case.messages]

    def provider(*args, **kwargs):
        assert not kwargs['abort_check']()
        if failure == 'failed':
            raise RuntimeError('hermetic provider failure')
        if failure == 'cancelled':
            case.task['aborted'] = True
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '' if failure == 'empty' else 'PARTIAL_SUMMARY'},
            'length' if failure == 'incomplete' else 'stop',
            {'prompt_tokens': 123, 'completion_tokens': 7})

    case.provider.side_effect = provider
    assert not reactive.reactive_compact(
        case.messages, case.task, error_text='request too large',
        byte_target=1 if failure == 'memory' else None)
    assert case.messages == original
    assert [id(message) for message in case.messages] == identities
    assert case.provider.call_count == (0 if failure == 'memory' else 1)
    assert len(case.snapshots) == 1
    source, archive_kwargs = case.snapshots[0]
    assert source == original and archive_kwargs['round_num'] == 12
    assert archive_kwargs['user_id'] == 7 and archive_kwargs['trigger'] == 'reactive'
    case.store.update_archive_summary.assert_called_once()
    args, kwargs = case.store.update_archive_summary.call_args
    assert args == ('runtime-archive', '', reactive._estimate_total_tokens(original), len(original))
    assert kwargs['post_messages'] is None and kwargs['user_id'] == 7
    assert kwargs['archive_scope'] == case.task.get('_archiveScope', '')
    receipt = kwargs['receipt']
    assert receipt['status'] == ('cancelled' if failure == 'cancelled' else 'failed')
    assert receipt['continuation']['format'] == 'none'
    assert receipt['summary']['accepted'] is False
    assert receipt['summary']['complete'] is False
    assert receipt['summary']['chars'] == 0
    if failure in {'empty', 'incomplete'}:
        assert receipt['summary']['usage']['inputTokens'] == 123
        assert receipt['summary']['usage']['outputTokens'] == 7
    if failure == 'incomplete':
        assert case.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_incomplete'

        assert receipt['outcomeReason'] == 'summary_incomplete'
    case.store.get_compaction_archive.assert_not_called()
    done = [event for event in case.events if event.get('type') == 'compaction_done']
    assert len(done) == 1 and done[0]['receipt'] == receipt
    assert done[0]['roundNum'] == 12 and done[0]['msgsAfter'] == len(original)


def test_runtime_complete_summary_has_no_deadline_or_post_summary_trim(reactive_runtime):
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff

    case = reactive_runtime
    original = deepcopy(case.messages)

    def provider(*args, **kwargs):
        assert not kwargs['abort_check']()
        case.clock[0] += 86_400  # Simulate a day without any wall-clock sleep.
        assert not kwargs['abort_check'](), 'Elapsed time must never abort a summary'
        assert 'deadline' not in kwargs
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nPreserve constraints and continue.'},
            'stop', {'prompt_tokens': 123, 'completion_tokens': 7})

    case.provider.side_effect = provider
    assert reactive.reactive_compact(case.messages, case.task, error_text='request too large')
    case.provider.assert_called_once()
    assert len(case.snapshots) == 1 and case.snapshots[0][0] == original
    assert case.messages != original
    assert any(is_compaction_handoff(message) for message in case.messages)
    # Accepted L2 alone owns its archive; reactive must neither read it back
    # for trimming nor emit another accepted continuation/snapshot.
    case.store.get_compaction_archive.assert_not_called()
    case.store.update_archive_summary.assert_called_once()
    args, kwargs = case.store.update_archive_summary.call_args
    assert args[0] == 'runtime-archive' and args[3] == len(case.messages)
    assert kwargs['archive_scope'] == case.task.get('_archiveScope', '')
    assert kwargs['post_messages'] == case.messages
    receipt = kwargs['receipt']
    assert receipt['summary']['accepted'] is True
    assert receipt['summary']['complete'] is True
    done = [event for event in case.events if event.get('type') == 'compaction_done']
    assert len(done) == 1 and done[0]['receipt'] == receipt
    assert done[0]['roundNum'] == 12
    snapshot = deepcopy(kwargs['post_messages'])
    case.messages[-1]['content'] = 'subsequent task mutation'
    assert kwargs['post_messages'] == snapshot


def test_runtime_archive_write_failure_does_not_claim_finalization(reactive_runtime):
    from lib.llm.stream_result import ProviderStreamResult

    case = reactive_runtime
    original = deepcopy(case.messages)
    case.provider.return_value = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'incomplete'}, 'length', {})
    case.store.update_archive_summary.side_effect = RuntimeError('hermetic archive unavailable')
    assert not reactive.reactive_compact(case.messages, case.task)
    assert case.messages == original
    case.store.update_archive_summary.assert_called_once()
    assert case.store.update_archive_summary.call_args.kwargs['post_messages'] is None
    assert not any(event.get('type') == 'compaction_done' for event in case.events)
