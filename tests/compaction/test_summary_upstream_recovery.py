"""Waitable upstream failures retain one authorized summary until recovery/Stop.

Exercise the real summary, compiler, mint, dispatcher, picker and slot lifecycle
with synthetic provider IO, clocks and archive/event sinks; no live model calls.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import RateLimitError
from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
from lib.tasks_pkg.compaction._constants import (
    _SUMMARY_DISPATCH_MAX_TOKENS, _SUMMARY_MAX_TOKENS,
)
from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
from tests.compaction.test_auxiliary_summary_integration import native as native
from tests.compaction.test_summary_billing_stop import billing as billing
from tests.compaction.test_summary_credential_health import _real_dispatch
from tests.compaction.test_summary_credential_resilience import (
    _PARENT_PIN, _active, _complete,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def recovering(native, billing, monkeypatch):
    from lib.llm_dispatch import _api_retry_state, _api_stream, dispatcher, slot
    from lib.tasks_pkg.compaction import _summary_budget

    native.task.update(id='summary-recovery', convId='summary-recovery-conv', aborted=False)
    clock = SimpleNamespace(now=10000.0, waits=[], on_wait=None)
    clock.time = clock.monotonic = lambda: clock.now
    for module in (_api_stream, _api_retry_state, dispatcher, slot, _summary_budget):
        monkeypatch.setattr(module, 'time', clock)
    # Exercise the ordinary dispatch defaults regardless of host overrides.
    monkeypatch.delenv('TOFU_429_SATURATION_SECS', raising=False)
    monkeypatch.delenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', raising=False)
    calls = []
    native.respond = lambda body, **kwargs: _complete(_active(native))

    def transport(body, **kwargs):
        calls.append((_active(native), deepcopy(body)))
        return native.respond(body, **kwargs)

    _real_dispatch(native, monkeypatch, transport)

    def wait(state, seconds, *, abort_check=None):
        clock.waits.append(seconds)
        clock.now += seconds
        if clock.on_wait is not None:
            clock.on_wait()

    monkeypatch.setattr(_api_stream, '_sleep_and_record_queue_wait', wait)
    native.clock, native.calls = clock, calls
    return native


def _summarize(native, *, quoted):
    usage = {}
    with provider_pin(_PARENT_PIN):
        quote = None
        if quoted:
            tokens = _estimate_total_tokens([
                {'role': 'system', 'content': native.summary._SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': 'fixed input'},
            ])
            quote = select_summary_route(
                native.task, tokens, _SUMMARY_MAX_TOKENS,
                capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
        result = native.summary._generate_query_aware_summary(
            native.source, 'continue', task=native.task, usage_out=usage,
            expected_route=quote)
        assert get_pinned_provider() == _PARENT_PIN
    return result, usage, quote


@pytest.mark.parametrize('lifecycle', ['live', 'transient', 'manual'])
@pytest.mark.parametrize('quoted', [False, True])
@pytest.mark.parametrize('status', [429, 503])
def test_summary_survives_repeated_upstream_rejections(
        recovering, lifecycle, quoted, status):
    native = recovering
    if lifecycle == 'transient':
        native.task.pop('id')
        native.task.pop('convId')
        native.task['_transientRuntime'] = True
    elif lifecycle == 'manual':
        for key in ('id', 'convId', 'aborted'):
            native.task.pop(key)
    before_source, before_config = deepcopy(native.source), deepcopy(native.task['config'])

    def respond(body, **kwargs):
        if len(native.calls) <= 4:
            raise RateLimitError(
                'Synthetic temporary upstream rejection', status_code=status,
                is_gateway=status == 503, retry_after_s=3)
        return _complete(_active(native))

    native.respond = respond
    result, usage, quote = _summarize(native, quoted=quoted)

    assert result == '### Objective\nPreserve the authorized work.'
    assert len(native.calls) == 5
    first_slot, first_body = native.calls[0]
    assert all(slot is first_slot and body == first_body for slot, body in native.calls)
    assert native.clock.waits, 'the real picker must wait for the provider cooldown'
    assert native.source == before_source and native.task['config'] == before_config
    assert native.task['model'] == 'gpt-6.1-sol'
    assert native.pool.slots == [native.parent]
    assert first_slot.api_key == '' and first_slot.inflight == 0
    assert len(native.state.resolved) == 1, 'retries retain the original authorized grant'
    assert len(usage['_summaryCalls']) == 1
    assert usage['prompt_tokens'] == 20 and usage['completion_tokens'] == 10
    assert [row['status'] for row in usage['_summaryAttempts']] == ['succeeded']
    assert '_summaryBudgetFailure' not in native.task.get('_summaryAttemptBudget', native.task)
    if quoted:
        assert usage['_summaryRoute']['routeIdentity'] == quote['routeIdentity']


@pytest.mark.parametrize('quoted', [False, True])
def test_owner_stop_during_rate_limit_wait_cleans_up_without_replacing_source(
        recovering, quoted):
    native = recovering
    before_source = deepcopy(native.source)

    def respond(body, **kwargs):
        raise RateLimitError('Synthetic TPM limit', status_code=429, retry_after_s=3)

    def stop_after_repeated_rejections():
        if len(native.calls) == 4:
            native.task['aborted'] = True

    native.respond = respond
    native.clock.on_wait = stop_after_repeated_rejections
    result, usage, _ = _summarize(native, quoted=quoted)

    assert result is None
    assert len(native.calls) == 4
    assert native.task['aborted'] is True
    assert native.source == before_source
    assert native.pool.slots == [native.parent]
    assert all(slot.api_key == '' and slot.inflight == 0 for slot, _ in native.calls)
    assert native.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_cancelled'
    assert not usage.get('_summaryCalls') and not usage.get('_summaryFailure')


def test_explicit_main_summary_policy_keeps_waiting_on_the_parent_route(recovering):
    native = recovering
    native.task['config']['compaction']['summaryPolicy'] = 'main'

    def respond(body, **kwargs):
        if len(native.calls) <= 4:
            raise RateLimitError('Synthetic TPM limit', status_code=429, retry_after_s=3)
        return _complete(_active(native))

    native.respond = respond
    result, usage, _ = _summarize(native, quoted=False)
    assert result == '### Objective\nPreserve the authorized work.'
    assert len(native.calls) == 5
    assert all(slot is native.parent for slot, _ in native.calls)
    assert native.parent.inflight == 0
    assert not native.state.resolved
    assert len(usage['_summaryCalls']) == 1


@pytest.mark.parametrize('status', [429, 503])
def test_recovered_summary_reduces_context_and_settles_one_accepted_receipt(
        recovering, monkeypatch, status):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff

    native = recovering
    source = [
        {'role': 'system', 'content': 'Required system policy.'},
        {'role': 'user', 'content': 'Preserve the authorized work.'},
        {'role': 'assistant', 'content': 'cold finding ' * 12000},
        {'role': 'user', 'content': 'Continue the authorized work.'},
    ]
    original = deepcopy(source)
    archived, stored, events = [], [], []

    def respond(body, **kwargs):
        assert source == original, 'no source replacement while the summary is retrying'
        if len(native.calls) <= 4:
            raise RateLimitError('Synthetic upstream rejection', status_code=status,
                                 is_gateway=status == 503, retry_after_s=3)
        return _complete(_active(native))

    def archive(conv_id, messages, **kwargs):
        assert kwargs['user_id'] == 41
        archived.append(deepcopy(messages))
        return 'recovered-summary-archive'

    def update_archive(archive_id, summary, tokens, count, **kwargs):
        stored.append((archive_id, summary, tokens, count, kwargs))

    native.respond = respond
    monkeypatch.setattr(compact, '_archive_transcript', archive)
    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: SimpleNamespace(
        update_archive_summary=update_archive))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr('lib.goal_runs.run_pins.update_objective_from_receipt', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.record_l2_compaction', lambda *a, **k: None)
    meta = {}
    with provider_pin(_PARENT_PIN):
        assert compact.force_compact_if_needed(
            source, task=native.task, force=True, preserve_budget_tokens=1000,
            _compaction_trigger='dispatch_guard', _result_meta=meta)
        assert get_pinned_provider() == _PARENT_PIN

    assert len(native.calls) == 5
    assert archived == [original]
    assert original[0] in source and original[-1] in source
    assert any(is_compaction_handoff(message) for message in source)
    assert meta['compacted'] and not meta['summaryFallback']
    assert len(stored) == 1
    assert stored[0][-1]['post_messages'] == source
    assert stored[0][-1]['user_id'] == 41
    [done] = [event for event in events if event['type'] == 'compaction_done']
    assert done['tokensAfter'] < done['tokensBefore'] and done['reductionPct'] > 0
    assert done['receipt']['status'] == 'completed'
    assert done['receipt']['summary']['accepted'] is True
    assert done['receipt']['summary']['complete'] is True
    assert done['receipt'] == stored[0][-1]['receipt']
    assert native.pool.slots == [native.parent]
    assert all(slot.api_key == '' and slot.inflight == 0 for slot, _ in native.calls)
