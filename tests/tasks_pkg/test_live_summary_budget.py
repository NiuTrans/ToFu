"""Live compaction cannot repeat paid failures across admission stages."""
from copy import deepcopy
import logging

import pytest

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.compaction import _summary_budget as policy
from lib.tasks_pkg.compaction._layer2 import _summary as summary

pytestmark = pytest.mark.unit


@pytest.fixture
def summary_env(monkeypatch):
    import lib.llm_dispatch as dispatch
    from lib.tasks_pkg.compaction import _compaction_usage
    clock = [0.0]
    calls = []
    charged = []
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(_compaction_usage, 'record_compaction_usage',
                        lambda conv, usage, **kw: charged.append(deepcopy(usage)))
    task = {'id': 'live-summary', 'convId': 'summary-conversation', '_userId': 1,
            'aborted': False, 'model': 'test-model',
            'config': {'compaction': {'summaryBudgetSeconds': 2}}}
    return dispatch, clock, calls, charged, task


def result(content='', *, finish='stop', state=ProviderStreamState.PROVIDER_FINISHED):
    return ProviderStreamResult(
        message={'role': 'assistant', 'content': content, 'reasoning_content': 'private thought'},
        compatibility_finish_reason=finish,
        provider_finish_reason=finish if state == ProviderStreamState.PROVIDER_FINISHED else None,
        saw_finish_reason=state == ProviderStreamState.PROVIDER_FINISHED, state=state,
        usage={'prompt_tokens': 100, 'completion_tokens': 50,
               '_dispatch': {'model': 'observed-model', 'provider_id': 'observed-provider',
                             'ttft_ms': 500}})


@pytest.mark.parametrize('failure', ['empty', 'exception', 'unverified', 'scratchpad'])
def test_failed_input_is_not_dispatched_again(summary_env, monkeypatch, failure, caplog):
    dispatch, clock, calls, charged, task = summary_env
    messages = [{'role': 'user', 'content': 'Preserve the authorized objective.'}]
    before = deepcopy(messages)

    def fail(body, **kwargs):
        calls.append(body)
        assert kwargs['thinking_enabled'] is False
        assert kwargs['max_429_attempts'] == 2
        clock[0] += 0.5
        if failure == 'exception':
            raise RuntimeError('injected')
        if failure == 'unverified':
            return result('partial', state=ProviderStreamState.PREMATURE_CLOSE)
        return result('<analysis>private thought</analysis>' if failure == 'scratchpad' else '')

    monkeypatch.setattr(dispatch, 'dispatch_stream', fail)
    with caplog.at_level(logging.INFO, logger=summary.__name__):
        for _ in range(3):
            assert summary._generate_query_aware_summary(messages, 'continue', task=task) is None
    assert len(calls) == 1
    assert messages == before
    assert task['aborted'] is False
    assert task['_summaryAttemptBudget']['_summaryBudgetFailure'] == (
        'summary_incomplete' if failure == 'unverified' else 'summary_dispatch_failed')
    assert len(charged) == (0 if failure == 'exception' else 1)
    if failure != 'exception':
        assert 'model=observed-model provider=observed-provider' in caplog.text
        assert 'thinking_chars=15' in caplog.text
        assert 'private thought' not in caplog.text


def test_slow_complete_summary_ignores_legacy_deadline(summary_env, monkeypatch):
    dispatch, clock, calls, charged, task = summary_env

    def saturated(body, **kwargs):
        calls.append(body)
        clock[0] += 600
        assert not kwargs['abort_check']()
        return result('### Objective\nComplete after provider wait.')

    monkeypatch.setattr(dispatch, 'dispatch_stream', saturated)
    for _ in range(3):
        assert summary._generate_query_aware_summary([], 'continue', task=task)
    assert len(calls) == 3 and clock[0] == 1800
    assert '_summaryBudgetFailure' not in task['_summaryAttemptBudget']
    assert task['aborted'] is False


@pytest.mark.parametrize('change', ['evidence', 'route', 'owner'])
def test_changed_request_can_recover_without_retaining_transcript(summary_env, monkeypatch, change):
    from lib.llm_dispatch.provider_pin import provider_pin
    dispatch, clock, calls, charged, task = summary_env
    messages = [{'role': 'user', 'content': 'original evidence'}]

    def respond(body, **kwargs):
        calls.append(body)
        return result('### Objective\nContinue the authorized work.' if len(calls) > 1 else '')

    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    assert summary._generate_query_aware_summary(messages, 'continue', task=task) is None
    if change == 'evidence':
        messages.append({'role': 'user', 'content': 'new binding evidence'})
    elif change == 'owner':
        task = dict(task, _userId=2, id='other-live-summary', convId='other-conversation')
        task.pop('_summaryAttemptBudget')
        task.pop('_principalContext', None)
    with provider_pin('changed-route' if change == 'route' else ''):
        assert summary._generate_query_aware_summary(messages, 'continue', task=task)
    assert len(calls) == 2
    state = task['_summaryAttemptBudget']
    assert len(state['requestKey']) == 64
    assert 'original evidence' not in repr(state)
    assert len(repr(state)) < 256


@pytest.mark.parametrize('late', [False, True])
def test_owner_stop_rejects_but_late_complete_output_is_adopted(summary_env, monkeypatch, late):
    dispatch, clock, calls, charged, task = summary_env

    def respond(body, **kwargs):
        calls.append(body)
        if late:
            clock[0] = 3
        else:
            task['aborted'] = True
        return result('### Objective\nMust not be adopted.')

    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    output = summary._generate_query_aware_summary([], 'continue', task=task)
    assert bool(output) is late
    assert len(charged) == 1
    assert len(calls) == 1


def test_preexisting_stop_spends_no_model_call(summary_env, monkeypatch):
    dispatch, clock, calls, charged, task = summary_env
    task['aborted'] = True
    monkeypatch.setattr(dispatch, 'dispatch_stream', lambda *a, **kw: pytest.fail('paid after Stop'))
    assert summary._generate_query_aware_summary([], 'continue', task=task) is None


def test_proactive_failure_then_final_recovery_pays_once(summary_env, monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    import lib.tasks_pkg.manager as manager
    dispatch, clock, calls, charged, task = summary_env
    monkeypatch.setattr(manager, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **kw: None)
    monkeypatch.setattr(l2, '_summary_cooldowns', {})

    def empty(body, **kwargs):
        calls.append(body)
        return result()

    monkeypatch.setattr(dispatch, 'dispatch_stream', empty)
    messages = [{'role': 'system', 'content': 'binding policy'}]
    for i in range(4):
        messages.extend([{'role': 'user', 'content': f'authorized request {i}'},
                         {'role': 'assistant', 'content': f'observation {i}'}])
    original = deepcopy(messages)
    options = dict(task=task, force=True, keep_recent_pairs=2,
                   _compaction_skip_archive=True)
    assert l2.force_compact_if_needed(messages, **options) is False
    assert messages == original
    assert l2.force_compact_if_needed(
        messages, **options, _compaction_trigger='dispatch_guard',
        _allow_deterministic_summary_fallback=True) is False
    assert len(calls) == len(charged) == 1
    assert messages == original


def test_successful_stream_forwards_deltas_and_reports_text_latency(summary_env, monkeypatch, caplog):
    dispatch, clock, calls, charged, task = summary_env
    deltas = []
    def respond(body, **kwargs):
        clock[0] = 0.5
        kwargs['on_content']('### Objective\n')
        clock[0] = 0.75
        kwargs['on_content']('Continue.')
        return result('### Objective\nContinue.')
    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    usage = {}
    with caplog.at_level(logging.INFO, logger=summary.__name__):
        assert summary._generate_query_aware_summary(
            [], 'continue', task=task, on_delta=deltas.append, usage_out=usage)
    assert deltas == ['### Objective\n', 'Continue.']
    assert 'first_text_ms=500.0' in caplog.text
    assert usage['completion_tokens'] == 50 and len(charged) == 1


def test_legacy_budget_zero_does_not_disable_complete_summaries(summary_env, monkeypatch):
    dispatch, clock, calls, charged, task = summary_env
    def respond(body, **kwargs):
        calls.append(body)
        return result('### Objective\nContinue.')
    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    task['config']['compaction']['summaryBudgetSeconds'] = 0
    assert summary._generate_query_aware_summary([], 'continue', task=task)
    task['config']['compaction']['summaryBudgetSeconds'] = 2
    assert summary._generate_query_aware_summary([], 'continue', task=task)
    assert len(calls) == 2


def test_live_successes_record_elapsed_without_a_deadline(summary_env, monkeypatch):
    dispatch, clock, calls, charged, task = summary_env
    def respond(body, **kwargs):
        calls.append(body)
        clock[0] += 1.25
        return result('### Objective\nContinue.')
    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    for _ in range(3):
        assert summary._generate_query_aware_summary([], 'continue', task=task)
    assert task['_summaryAttemptBudget']['_summaryBudgetSpentSeconds'] == 3.75
    assert summary._generate_query_aware_summary([], 'new evidence', task=task)
    assert len(calls) == 4


@pytest.mark.parametrize('can_disable,expected', [(True, 'none'), (False, 'low')])
def test_prebuilt_summary_compiles_minimum_reasoning_on_actual_wire(
        summary_env, monkeypatch, can_disable, expected):
    from types import SimpleNamespace
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    dispatch, clock, calls, charged, task = summary_env
    slot = SimpleNamespace(
        model='summary-test-model', provider_id='summary-test-provider',
        protocol='openai', thinking_format='', max_output_tokens=4096,
        inference_profile={'reasoning': {
            'support': 'supported', 'wire_format': 'reasoning_effort',
            'can_disable': can_disable,
            'supported_efforts': ['none', 'low', 'high', 'max'] if can_disable else ['low', 'high', 'max'],
            **({'disabled_effort': 'none'} if can_disable else {}),
            'default_effort': 'max'}})
    wires = []

    def respond(body, **kwargs):
        # Exercise the production prebuilt-body path, not just its kwargs.
        wire = _adapt_stream_body_for_slot(
            slot, body, True, tools=None,
            max_tokens=kwargs['max_tokens'], temperature=kwargs['temperature'],
            thinking_enabled=kwargs['thinking_enabled'], preset='max', effort=None)
        wires.append(wire)
        return result('### Objective\nContinue.')

    monkeypatch.setattr(dispatch, 'dispatch_stream', respond)
    assert summary._generate_query_aware_summary([], 'continue', task=task)
    assert len(wires) == 1
    assert wires[0].get('reasoning_effort') == expected
    # The dispatch budget is deliberately NOT the 2,200-token receipt cap
    # (enforced content-side): _adapt_stream_body_for_slot clamps the wire to
    # the slot's own max_output_tokens ceiling (4096 here).
    assert wires[0]['max_tokens'] == 4096
    assert wires[0]['_reasoning_resolution']['requested'] == 'off'
