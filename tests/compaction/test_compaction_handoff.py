"""Executable automatic-handoff contract at compaction and real wire boundaries."""
from copy import deepcopy
import json

import pytest

from lib.tasks_pkg.compaction._handoff import (
    HANDOFF_FIELD, HANDOFF_GUIDANCE, compaction_phase, finalize_handoff_order,
    is_compaction_handoff, make_compaction_handoff,
)
from lib.tasks_pkg.compaction._history import is_human_message
from lib.tasks_pkg.compaction._layer2 import _compact as l2
from lib.tasks_pkg.compaction._layer2._prompt import _extract_prior_receipt
from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.tool_history_pairing import adjacent_tool_call_result_pairs
from tests.compaction.test_compaction_history_wire import compact, tool_round, user
from tests.compaction.test_compaction_history_providers import _wire, _text, PROVIDERS

pytestmark = pytest.mark.unit
SUMMARY = '### Objective\nContinue scoped work.\n### Ruled Out / Dead Ends\nDo not retry unavailable route.'


@pytest.mark.parametrize('phase', ['pre_turn', 'mid_turn'])
def test_handoff_and_archive_placement_follow_machine_contract(phase):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

    schema = json.loads((Path(__file__).parents[2] /
        'contracts/compaction_handoff_v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    handoff = make_compaction_handoff(SUMMARY, phase=phase)
    validator.validate(handoff)
    assert '_isCompactionSummary' not in handoff  # manual persistence is separate
    for key in ('role', '_isMeta', HANDOFF_FIELD):
        invalid = deepcopy(handoff)
        invalid.pop(key)
        assert not validator.is_valid(invalid)
    receipt = build_compaction_receipt(
        trigger='force', status='completed', strategy='selective_summary',
        implementation='model_summary', continuation_format='context_handoff',
        continuation_phase=phase, summary_generated=True, summary_text=SUMMARY)
    validator = Draft202012Validator({'$ref': '#/$defs/automaticContinuation',
                                     '$defs': schema['$defs']})
    validator.validate(receipt['continuation'])
    assert SUMMARY not in json.dumps(receipt)
    receipt['continuation']['phase'] = 'pre_turn' if phase == 'mid_turn' else 'mid_turn'
    assert not validator.is_valid(receipt['continuation'])


def fixture_messages(phase):
    messages = [user('Original scoped task'),
                {'role': 'assistant', 'content': 'old result ' * 7000}, user('Continue scoped work.')]
    for index in range(12):
        messages.extend(tool_round(index, size=10, count=2))
    if phase == 'pre_turn':
        messages.append(user('Stop editing; report only.'))
    return messages


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('phase', ['pre_turn', 'mid_turn'])
def test_real_compaction_reaches_each_provider_without_fake_tool_execution(compact, provider, phase):  # noqa: F811
    messages = fixture_messages(phase)
    original = deepcopy(messages)
    current = next(m['content'] for m in reversed(messages) if is_human_message(m))
    meta = compact(messages, budget=1)
    handoffs = [m for m in messages if is_compaction_handoff(m)]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert meta['handoff_phase'] == phase == handoff[HANDOFF_FIELD]['phase']
    assert not is_human_message(handoff)
    assert not any(c['function']['name'] == 'context_compact'
                   for c, _ in adjacent_tool_call_result_pairs(messages))
    assert [(c['id'], r['tool_call_id']) for c, r in adjacent_tool_call_result_pairs(messages)] == [
        ('call-11-0', 'call-11-0'), ('call-11-1', 'call-11-1')]
    for m in messages:
        if m.get('tool_calls') or m.get('tool_call_id'):
            assert m in original
    if phase == 'mid_turn':
        assert messages[-1] is handoff
    else:
        assert messages[-2] is handoff and messages[-1] == user(current)
    items = _wire(messages, provider)
    rendered = '\n'.join(_text(item) for item in items)
    assert rendered.count(HANDOFF_GUIDANCE) == 1
    assert rendered.count(current) == 1
    if phase == 'mid_turn':
        assert rendered.rstrip().endswith('</compaction_handoff>')
    else:
        assert rendered.rstrip().endswith(current)
        assert rendered.index('</compaction_handoff>') < rendered.index(current)
    payload = json.dumps(items)
    assert 'context_compact' not in payload and HANDOFF_FIELD not in payload
    assert meta['tokens_after_estimated'] == l2._estimate_total_tokens(messages)


def test_recomposition_keeps_summary_last_only_before_new_execution_or_steering():
    handoff = make_compaction_handoff(SUMMARY, phase='mid_turn')
    messages = [user('Work'), *tool_round(1), handoff]
    blocks = [context_block('platform_static', 'Required policy'),
              context_block('environment', 'current directory')]
    render_context(messages, blocks, ComposeRequest())
    assert messages[0]['role'] == 'system' and messages[-1] is handoff
    frozen = deepcopy(messages)
    render_context(messages, blocks, ComposeRequest())
    assert messages == frozen
    messages.append(user('Stop now.'))
    render_context(messages, [context_block('environment', 'new directory')], ComposeRequest())
    assert messages.index(handoff) < messages.index(user('Stop now.'))
    assert compaction_phase(messages) == 'pre_turn'
    messages.extend(tool_round(2))
    render_context(messages, [context_block('environment', 'third directory')], ComposeRequest())
    assert messages.index(handoff) < next(i for i, m in enumerate(messages)
                                        if m.get('tool_call_id') == 'call-2-0')


def test_chronological_inbox_evidence_is_not_moved_behind_old_summary():
    handoff = make_compaction_handoff(SUMMARY, phase='mid_turn')
    messages = [handoff, user('new background result', _isMeta=True, _isInboxInject=True),
                user('required context', _contextComposer=True, _isMeta=True)]
    frozen = deepcopy(messages)
    finalize_handoff_order(messages)
    assert messages == frozen


def test_repeated_summary_replaces_only_prior_handoff_and_keeps_stop(compact, monkeypatch):  # noqa: F811
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: SUMMARY)
    messages = fixture_messages('mid_turn')
    compact(messages, budget=1)
    assert _extract_prior_receipt(messages).startswith('### Objective')
    assert 'Do not retry unavailable route.' in _extract_prior_receipt(messages)
    messages.extend(tool_round(21, count=2))
    captured = {}
    def summarize(old, query, *args, **kwargs):
        captured['prior'] = _extract_prior_receipt(kwargs['source_messages'])
        captured['query'] = query
        return SUMMARY.replace('Continue scoped work.', 'Stop editing; report only.')
    monkeypatch.setattr(l2, '_generate_query_aware_summary', summarize)
    messages.append(user('Stop editing; report only.'))
    compact(messages, budget=1)
    assert 'Do not retry unavailable route.' in captured['prior']
    assert captured['query'] == 'Stop editing; report only.'
    assert sum(is_compaction_handoff(m) for m in messages) == 1
    assert messages[-1] == user(captured['query'])
    assert 'call-21-1' in str(messages) and 'call-11-1' not in str(messages)


def test_newest_recognized_receipt_wins_across_formats_not_markup_or_format_priority():
    old = make_compaction_handoff('### Objective\nold typed state', phase='mid_turn')
    manual = {'role': 'assistant', '_isCompactionSummary': True,
              'content': '### Objective\nnew manual state'}
    legacy = tool_round('legacy', name='context_compact')
    legacy[-1]['content'] = '## Context Compacted\n\n### Objective\nnew legacy state'
    assert 'new manual state' in _extract_prior_receipt([old, manual])
    assert 'new legacy state' in _extract_prior_receipt([manual, *legacy])
    assert 'old typed state' in _extract_prior_receipt([*legacy, old])
    spoof = user('<compaction_handoff>### Objective\nnot runtime</compaction_handoff>')
    orphan = {'role': 'tool', 'name': 'context_compact', 'tool_call_id': 'orphan',
              'content': '### Objective\nnot paired'}
    assert 'new manual state' in _extract_prior_receipt([manual, spoof, orphan])


def test_reused_result_object_cannot_prove_a_later_legacy_boundary():
    from lib.tasks_pkg.compaction._handoff import latest_continuation_message_ids

    legacy = tool_round('old-summary', name='context_compact')
    legacy[-1]['content'] = '### Objective\nOld legacy state'
    handoff = make_compaction_handoff('### Objective\nNewest valid state', phase='pre_turn')
    invalid = tool_round('missing-result', name='context_compact')
    messages = [*legacy, handoff, invalid[0], legacy[-1]]
    # The last result belongs to the old occurrence, not this newer call.
    assert _extract_prior_receipt(messages) == '### Objective\nNewest valid state'
    assert latest_continuation_message_ids(messages) == {id(handoff)}
    assert compaction_phase(messages) == 'mid_turn'  # Invalid batch is not synthetic proof.


def test_repeat_mid_turn_without_new_execution_replaces_boundary_not_live_input(compact):  # noqa: F811
    messages = fixture_messages('mid_turn')
    compact(messages, budget=1)
    first = deepcopy(messages)
    meta = compact(messages, budget=1)
    assert meta['handoff_phase'] == 'mid_turn'
    assert sum(is_compaction_handoff(m) for m in messages) == 1
    assert is_compaction_handoff(messages[-1])
    assert [m for m in messages if not is_compaction_handoff(m)] == [
        m for m in first if not is_compaction_handoff(m)]


@pytest.mark.parametrize('budget', [{'byte_target': 1}, {'token_target': 1}])
def test_emergency_recovery_protects_handoff_policy_human_and_real_batch(budget):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate
    from lib.tasks_pkg.compaction._reactive._strip import _truncate_largest_message
    handoff = make_compaction_handoff(SUMMARY * 1000, phase='mid_turn')
    messages = [user('opening'), {'role': 'assistant', 'content': 'cold' * 5000},
                user('Stop; no deployment.'), *tool_round(4, size=1), handoff]
    protected = deepcopy([messages[0], *messages[2:]])
    assert _head_truncate(messages, **budget) == 1
    assert messages == protected
    assert _truncate_largest_message(messages, ceiling_chars=500) == (-1, 0)
    assert messages == protected


def test_open_multicall_batch_defers_without_paid_summary_or_source_mutation(compact, monkeypatch):  # noqa: F811
    messages = fixture_messages('mid_turn') + tool_round(13, count=2)[:-1]
    # Recycled IDs must not borrow a previous batch's second result.
    for call in messages[-2]['tool_calls']:
        call['id'] = 'same'
    messages[-1]['tool_call_id'] = 'same'
    original = deepcopy(messages)
    calls = []
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: calls.append(1))
    meta = {}
    assert not l2.force_compact_if_needed(messages, force=True, _result_meta=meta)
    assert messages == original and not calls and meta['reason'] == 'open_tool_batch'


@pytest.mark.parametrize('case', ['orphan', 'interrupted', 'reused_object'])
def test_orphan_results_defer_lossy_compaction_without_borrowing_old_calls(compact, monkeypatch, case):  # noqa: F811
    from lib.tasks_pkg.compaction._handoff import has_open_tool_batch
    from lib.tasks_pkg.compaction import _reactive as reactive

    messages = fixture_messages('mid_turn')
    prior = tool_round('recycled')
    messages.extend(prior)
    if case == 'orphan':
        messages.extend([user('Later steering'), deepcopy(prior[-1])])
    elif case == 'reused_object':
        messages.extend([user('Later steering'), prior[-1]])
    else:
        interrupted = tool_round('interrupted')
        messages.extend([interrupted[0], user('runtime event', _isMeta=True), interrupted[1]])
    before = deepcopy(messages)
    calls = []
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: calls.append('summary'))
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **k: calls.append('archive'))
    assert has_open_tool_batch(messages)
    meta = {}
    assert not l2.force_compact_if_needed(messages, force=True, _result_meta=meta)
    assert meta['reason'] == 'open_tool_batch'
    assert not reactive.reactive_compact(messages)
    assert messages == before and calls == []


def test_cancellation_during_summary_cannot_install_deterministic_or_model_handoff(compact, monkeypatch):  # noqa: F811
    messages = fixture_messages('mid_turn')
    original = deepcopy(messages)
    task = {'id': 'cancelled-summary', 'aborted': False, 'config': {}, '_userId': 1}
    def cancel(*a, **k):
        task['aborted'] = True
        return SUMMARY
    monkeypatch.setattr(l2, '_generate_query_aware_summary', cancel)
    assert not l2.force_compact_if_needed(messages, task, force=True,
                                        preserve_budget_tokens=1,
                                        _allow_deterministic_summary_fallback=True)
    assert messages == original


@pytest.mark.parametrize('stage', ['before_dispatch', 'recovery', 'attachments', 'archive'])
def test_cancellation_at_every_local_commit_boundary_preserves_messages(compact, monkeypatch, stage):  # noqa: F811
    messages = fixture_messages('mid_turn')
    original = deepcopy(messages)
    task = {'id': 'cancel-commit', 'aborted': stage == 'before_dispatch',
            'config': {}, '_userId': 1}
    calls = []
    def summarize(*a, **k):
        calls.append('summary')
        return None if stage == 'recovery' else SUMMARY
    def cancel(*a, **k):
        task['aborted'] = True
        return SUMMARY if stage == 'recovery' else [] if stage == 'attachments' else None
    monkeypatch.setattr(l2, '_generate_query_aware_summary', summarize)
    if stage != 'before_dispatch':
        monkeypatch.setattr(l2, {
            'recovery': '_deterministic_recovery_summary',
            'attachments': '_extract_recently_accessed_files',
            'archive': '_archive_transcript',
        }[stage], cancel)
    meta = {}
    assert not l2.force_compact_if_needed(
        messages, task, force=True, preserve_budget_tokens=1,
        _allow_deterministic_summary_fallback=True,
        _allow_head_truncate_fallback=True, _result_meta=meta)
    assert messages == original and not meta['compacted']
    assert meta['reason'] == 'aborted'
    assert calls == ([] if stage == 'before_dispatch' else ['summary'])


@pytest.mark.parametrize('task_kind', ['utility', 'live', 'transient'])
@pytest.mark.parametrize('stage', ['open_batch', 'before_work', 'archive', 'summary'])
def test_reactive_recovery_never_mutates_after_defer_or_cancellation(
        compact, monkeypatch, stage, task_kind):  # noqa: F811
    from lib.tasks_pkg.compaction import _reactive as reactive

    messages = fixture_messages('mid_turn')
    if stage == 'open_batch':
        messages.extend(tool_round(31, count=2)[:-1])
    original = deepcopy(messages)
    task = {'id': 'cancel-reactive', 'aborted': stage == 'before_work',
            'config': {}, '_userId': 1}

    if task_kind == 'live':
        task['convId'] = 'reactive-owner'
    elif task_kind == 'transient':
        task['_transientRuntime'] = True
    calls = []
    def archive(*a, **k):
        calls.append('archive')
        if stage == 'archive':
            task['aborted'] = True
        return None
    def summarize(*a, **k):
        calls.append('summary')
        task['aborted'] = True
        return False
    def unexpected(*a, **k):
        pytest.fail('Recovery must not mutate or run fallback beyond the closed cancellation boundary')
    monkeypatch.setattr(reactive, '_archive_transcript', archive)
    monkeypatch.setattr(reactive, '_get_context_limit', lambda task: 1_000_000)
    monkeypatch.setattr(reactive, 'force_compact_if_needed', summarize)
    monkeypatch.setattr(reactive, '_truncate_largest_message',
                        (lambda *a, **k: (-1, 0)) if stage == 'summary' else unexpected)
    monkeypatch.setattr(reactive, 'micro_compact',
                        (lambda *a, **k: None) if stage == 'summary' else unexpected)
    monkeypatch.setattr(reactive, '_head_truncate', unexpected)
    assert not reactive.reactive_compact(messages, task)
    assert messages == original
    assert calls == ([] if stage in {'open_batch', 'before_work'} else
                     ['archive'] if stage == 'archive' else ['archive', 'summary'])


@pytest.mark.parametrize('entry', ['local', 'reactive', 'utility_head_fallback'])
def test_cancelled_archive_closes_without_accepted_summary_or_post_snapshot(compact, monkeypatch, entry):  # noqa: F811
    from unittest.mock import MagicMock
    import lib.agent_core.store as stores
    import lib.tasks_pkg.manager as manager
    from lib.tasks_pkg.compaction import _reactive as reactive
    from lib.tasks_pkg.compaction import _tokens as tokens

    messages = fixture_messages('mid_turn')
    original = deepcopy(messages)
    task = {'id': 'cancelled-archive', 'convId': 'archive-owner', 'aborted': False,
            '_userId': 7, 'round_num': 12, 'config': {}}
    store, events = MagicMock(), []
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event', lambda t, e: events.append(e))
    def archive(*a, **k):
        task['aborted'] = True
        return 'cancelled-archive-id'
    monkeypatch.setattr(reactive if entry == 'reactive' else l2, '_archive_transcript', archive)
    if entry == 'utility_head_fallback':
        # Explicit non-live utility compatibility only. Live/transient callers
        # cannot activate this destructive fallback, even with the old flag.
        task.pop('id')
        monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: None)
        monkeypatch.setattr(l2, '_get_context_limit', lambda t: 100)
        monkeypatch.setattr(l2, '_usable_context', lambda t: 100)
        monkeypatch.setattr(tokens, '_count_tokens_authoritative', lambda *a, **k: (500, 'mock'))
    else:
        monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: SUMMARY)
    if entry == 'reactive':
        assert not reactive.reactive_compact(messages, task)
    else:
        assert not l2.force_compact_if_needed(messages, task, force=True,
            preserve_budget_tokens=1, _compaction_round=12, _allow_head_truncate_fallback=True)
    assert messages == original
    store.update_archive_summary.assert_called_once()
    args, kwargs = store.update_archive_summary.call_args
    assert args == ('cancelled-archive-id', '', l2._estimate_total_tokens(original), len(original))
    assert kwargs['user_id'] == 7 and kwargs['post_messages'] is None
    assert kwargs['receipt']['status'] == 'cancelled'
    assert kwargs['receipt']['continuation']['format'] == 'none'
    assert not kwargs['receipt']['summary']['accepted']
    # The COMPACTING phase broadcast rides the same append_event channel since
    # 67daf4749; the durable receipt remains exactly one event.
    receipts = [e for e in events if 'receipt' in e]
    assert len(receipts) == 1 and receipts[0]['roundNum'] == 12
    assert receipts[0]['receipt'] == kwargs['receipt']


@pytest.mark.parametrize('task_kind', ['utility', 'live', 'transient'])
def test_stop_after_adoption_does_not_relabel_the_accepted_archive(
        compact, monkeypatch, task_kind):  # noqa: F811
    from unittest.mock import MagicMock
    import lib.agent_core.store as stores
    from lib.tasks_pkg.compaction import _reactive as reactive

    messages = fixture_messages('mid_turn')
    task = {'id': 'stop-after-adoption', 'aborted': False, 'config': {}, '_userId': 1}

    if task_kind == 'live':
        task['convId'] = 'adopted-owner'
    elif task_kind == 'transient':
        task['_transientRuntime'] = True
    store = MagicMock()
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **k: 'already-adopted')
    monkeypatch.setattr(reactive, '_truncate_largest_message', lambda *a, **k: (-1, 0))
    monkeypatch.setattr(reactive, 'micro_compact', lambda *a, **k: None)
    def adopted(*a, **k):
        task['aborted'] = True
        return True
    monkeypatch.setattr(reactive, 'force_compact_if_needed', adopted)
    assert not reactive.reactive_compact(messages, task)
    store.update_archive_summary.assert_not_called()


@pytest.mark.parametrize('newest_format', ['typed', 'manual', 'legacy'])
@pytest.mark.parametrize('target', [{'byte_target': 1}, {'token_target': 1}])
def test_recovery_protects_newest_continuation_by_chronology_across_formats(
        newest_format, target):
    from lib.tasks_pkg.compaction._handoff import latest_continuation_message_ids
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate
    from lib.tasks_pkg.compaction._reactive._strip import _truncate_largest_message

    typed = [make_compaction_handoff(SUMMARY * 100, phase='mid_turn')]
    manual = [{'role': 'assistant', '_isCompactionSummary': True,
               'content': 'manual accepted state ' * 1000}]
    legacy = tool_round('legacy', name='context_compact')
    legacy[-1]['content'] = 'legacy accepted state ' * 1000
    receipts = {'typed': typed, 'manual': manual, 'legacy': legacy}
    ordered = [key for key in receipts if key != newest_format] + [newest_format]
    messages = [user('original'), *[m for key in ordered for m in receipts[key]],
                user('Stop; report only.'), *tool_round('real', size=1)]
    expected = deepcopy(receipts[newest_format])
    newest = latest_continuation_message_ids(messages)
    assert newest == {id(row) for row in receipts[newest_format]}
    assert _head_truncate(messages, **target) > 0
    assert [row for row in messages if id(row) in newest] == expected
    assert _truncate_largest_message(messages, ceiling_chars=500) == (-1, 0)
    assert [row for row in messages if id(row) in newest] == expected
    assert messages[-3] == user('Stop; report only.')


def test_handoff_and_runtime_context_cannot_make_active_structural_turn_evictable():
    from lib.tasks_pkg.compaction._steps import CompactionContext, MessageEditor
    from lib.tasks_pkg.tool_orchestration_policy import latest_user_text

    live = [user('Current human task'), *tool_round('active'),
            make_compaction_handoff(SUMMARY, phase='mid_turn'),
            user('runtime context', _isMeta=True, _contextComposer=True)]
    messages = [user('Old human request'), {'role': 'assistant', 'content': 'done'}, *live]
    editor = MessageEditor(CompactionContext(messages=messages))
    assert len(editor.turns()) == 2
    assert len(editor.evictable_turns()) == 1
    assert latest_user_text(messages) == 'Current human task'
    editor.drop_turns(editor.turns())
    assert messages == live
    assert editor.drop_turns(editor.turns()) == 0
    assert messages == live


def test_url_prefetch_cannot_treat_handoff_evidence_as_a_new_human_url(monkeypatch):
    from lib.tasks_pkg.executor._summary import _prefetch_user_urls
    import lib.search_runtime as search_runtime

    def forbidden_search_runtime():
        pytest.fail('runtime evidence must not activate URL prefetch')

    monkeypatch.setattr(search_runtime, 'ensure_search_runtime', forbidden_search_runtime)
    messages = [user('Report the result; do not fetch anything.'),
                make_compaction_handoff('Source: https://example.invalid/evidence',
                                        phase='mid_turn')]
    task = {'id': 'prefetch-provenance', 'toolRounds': []}
    assert _prefetch_user_urls(messages, task) == []
    assert task['toolRounds'] == []


@pytest.mark.parametrize('phase', ['pre_turn', 'mid_turn'])
def test_final_admission_counts_restored_state_and_preserves_phase(compact, monkeypatch, phase):  # noqa: F811
    from tests.goals.test_checklist_context import _task, _admit, _assert_current, _measure_chars, _text
    messages = fixture_messages(phase)
    compact(messages, budget=1)
    handoff = next(m for m in messages if is_compaction_handoff(m))
    task = _task()
    _measure_chars(monkeypatch)
    measurement = _admit(messages, task)
    _assert_current(messages, task)
    assert measurement['totalTokens'] == len(_text(messages))
    if phase == 'mid_turn':
        assert messages[-1] is handoff
    else:
        assert messages.index(handoff) < next(i for i, m in enumerate(messages)
                                              if is_human_message(m))


@pytest.mark.parametrize('memory_pressure', [False, True])
def test_reactive_retry_keeps_required_policy_and_handoff_boundary(compact, monkeypatch, memory_pressure):  # noqa: F811
    from lib.cgroup_guard import MemoryPressureError
    from lib.llm_errors import PromptTooLongError
    from lib.tasks_pkg.compaction import _reactive as reactive
    from lib.tasks_pkg.context_composer import _providers
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from tests.goals.test_checklist_context import _task, _assert_current, _text

    messages = fixture_messages('mid_turn')
    task = _task()
    task['config']['memoryEnabled'] = False
    required = [context_block('platform_static', 'runtime policy'),
                context_block('project_rules', 'Never deploy without scoped authorization.')]
    render_context(messages, required, ComposeRequest())
    monkeypatch.setattr(_providers, 'collect_context_blocks', lambda *a: required)
    # Public composer facade binds collect_context_blocks once.
    import lib.tasks_pkg.context_composer as composer
    monkeypatch.setattr(composer, 'collect_context_blocks', lambda *a: required)
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(reactive, 'micro_compact', lambda *a, **k: None)
    if memory_pressure:
        compact(messages, budget=1)
        # Remove reconstructible old prose, not required project context.
        messages.insert(1, {'role': 'assistant', 'content': 'cold history ' * 100000})
        cause = MemoryPressureError('memory headroom')
    else:
        monkeypatch.setattr(reactive, '_get_context_limit', lambda *a: 1000)
        cause = PromptTooLongError('too long')
    body = fallback._prepare_reactive_retry_body(
        task, {}, 'test-model', messages, [], 'medium', False, cause, 'test')
    assert body is not None
    _assert_current(body['messages'], task)
    assert 'Never deploy without scoped authorization.' in _text(body['messages'])
    assert _text(body['messages']).rstrip().endswith('</compaction_handoff>')


@pytest.mark.parametrize('target', [{'byte_target': 1}, {'token_target': 1}])
def test_recovery_resolves_instanced_context_without_losing_required_policy(target):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [user('original'), {'role': 'assistant', 'content': 'old state'},
                user('Current scoped work'), *tool_round('active')]
    render_context(messages, [
        context_block('project_rules', 'Required policy remains in force.'),
        context_block('modified_files', 'Optional file reminder.', instance='round7'),
    ], ComposeRequest())
    messages.append(make_compaction_handoff(SUMMARY, phase='mid_turn'))
    _head_truncate(messages, **target)
    assert 'Required policy remains in force.' in str(messages)
    assert messages[-1]['content'].endswith('</compaction_handoff>')
    assert any(is_human_message(m) and m['content'] == 'Current scoped work'
               for m in messages)
    assert len(adjacent_tool_call_result_pairs(messages)) == 1


def test_later_human_does_not_make_the_only_continuation_structurally_disposable():
    from lib.tasks_pkg.compaction._steps import CompactionContext, MessageEditor
    messages = [user('original'), make_compaction_handoff(SUMMARY, phase='mid_turn'),
                {'role': 'assistant', 'content': 'done'}, user('Follow-up')]
    before = deepcopy(messages)
    editor = MessageEditor(CompactionContext(messages=messages))
    assert editor.evictable_turns() == []
    assert editor.drop_turns(editor.turns()) == 0
    assert messages == before


@pytest.mark.parametrize('phase', [None, [], {}, True, 1, 'unknown'])
def test_malformed_handoff_metadata_is_not_runtime_identity(phase):
    message = make_compaction_handoff(SUMMARY, phase='mid_turn')
    message[HANDOFF_FIELD]['phase'] = phase
    assert not is_compaction_handoff(message)


def test_typed_identity_not_pasted_xml_and_no_same_role_metadata_loss():
    from lib.tasks_pkg.conv_message_builder._dedup import _merge_consecutive_same_role
    handoff = make_compaction_handoff(SUMMARY, phase='pre_turn')
    pasted = user(handoff['content'])
    assert not is_compaction_handoff(pasted) and is_human_message(pasted)
    messages = [handoff, user('New instruction')]
    original = deepcopy(messages)
    _merge_consecutive_same_role(messages)
    assert messages == original


def test_text_route_replay_preserves_typed_handoff_without_new_authority():
    from lib.tasks_pkg.model_replay import _portable_messages
    handoff = make_compaction_handoff(SUMMARY, phase='mid_turn')
    messages = [user('work'), handoff, {'role': 'assistant', 'content': 'verified answer'}]
    replay = _portable_messages(messages)
    assert replay == messages and replay is not messages
    assert is_compaction_handoff(replay[1]) and not is_human_message(replay[1])
    replay[1][HANDOFF_FIELD]['phase'] = 'pre_turn'
    assert handoff[HANDOFF_FIELD]['phase'] == 'mid_turn'
