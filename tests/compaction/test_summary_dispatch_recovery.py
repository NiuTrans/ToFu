"""Summary retries preserve source, authority and already-observed provider work."""
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def summary_transport(monkeypatch):
    import lib.llm_dispatch as dispatch
    from lib.tasks_pkg.compaction import _compaction_usage, _summary_cost
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(_summary_cost, 'price_summary_call',
                        lambda *a, **kw: {'known': False, 'costUsd': None})
    charged = []
    monkeypatch.setattr(_compaction_usage, 'record_compaction_usage',
                        lambda conv, usage, **kw: charged.append(deepcopy(usage)))
    return summary, dispatch, charged


@pytest.mark.parametrize('streaming', [False, True])
def test_unqualified_summary_retries_transport_failure_once(summary_transport, monkeypatch, streaming):
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

    summary, dispatch, charged = summary_transport
    calls = []
    content = '### Objective\nPreserve all authorized work and its evidence.'
    usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'finish_reason': 'stop'}

    def respond(body, **kwargs):
        calls.append((deepcopy(body), kwargs))
        if len(calls) == 1:
            raise RuntimeError('injected unavailable cheap transport')
        if streaming:
            return ProviderStreamResult(
                message={'role': 'assistant', 'content': content}, usage=usage,
                state=ProviderStreamState.PROVIDER_FINISHED,
                compatibility_finish_reason='stop', provider_finish_reason='stop',
                saw_finish_reason=True)
        return content, usage

    monkeypatch.setattr(dispatch, 'dispatch_stream' if streaming else 'dispatch_chat', respond)
    messages = [{'role': 'user', 'content': 'Preserve the authorized objective.'}]
    original = deepcopy(messages)
    result = summary._generate_query_aware_summary(
        messages, 'continue', on_delta=(lambda delta: None) if streaming else None)
    assert result == content
    assert [options['capability'] for _, options in calls] == ['cheap', 'text']
    assert calls[0][0] == calls[1][0]
    assert len(charged) == 1
    assert messages == original


def test_failed_text_retry_does_not_repeat_or_mutate_source(summary_transport, monkeypatch):
    summary, dispatch, charged = summary_transport
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs['capability'])
        raise RuntimeError('injected transport outage')

    monkeypatch.setattr(dispatch, 'dispatch_chat', unavailable)
    messages = [{'role': 'user', 'content': 'Keep this instruction intact.'}]
    original = deepcopy(messages)
    assert summary._generate_query_aware_summary(messages, 'continue') is None
    assert calls == ['cheap', 'text']
    assert charged == []
    assert messages == original


@pytest.mark.parametrize('error_kind', ['cancelled', 'payload', 'incomplete'])
def test_nonretryable_failures_never_buy_another_summary(summary_transport, monkeypatch, error_kind):
    from lib.llm.llm_errors import AbortedError, LocalRequestPreparationError

    summary, dispatch, charged = summary_transport
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs['capability'])
        if error_kind == 'cancelled':
            raise AbortedError('owner stopped')
        if error_kind == 'payload':
            raise LocalRequestPreparationError('invalid request schema')
        return 'partial', {'prompt_tokens': 100, 'completion_tokens': 20,
                           'finish_reason': 'length'}

    monkeypatch.setattr(dispatch, 'dispatch_chat', fail)
    assert summary._generate_query_aware_summary([], 'continue') is None
    assert calls == ['cheap']
    assert len(charged) == (1 if error_kind == 'incomplete' else 0)


def test_accounting_error_after_provider_completion_never_replays(summary_transport, monkeypatch):
    from lib.tasks_pkg.compaction import _summary_cost

    summary, dispatch, _charged = summary_transport
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs['capability'])
        return '### Objective\nKeep the result.', {'finish_reason': 'stop'}

    def broken_accounting(*args, **kwargs):
        raise RuntimeError('injected local accounting failure')

    monkeypatch.setattr(dispatch, 'dispatch_chat', complete)
    monkeypatch.setattr(_summary_cost, 'price_summary_call', broken_accounting)
    assert summary._generate_query_aware_summary([], 'continue') is None
    assert calls == ['cheap']


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('recovers', [False, True])
def test_transient_credential_recovery_retains_exact_summary_route(
        summary_transport, monkeypatch, streaming, recovers):
    from types import SimpleNamespace
    from lib.llm.llm_errors import LocalRequestPreparationError, SubscriptionCredentialUnavailableError
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
    from lib.tasks_pkg.compaction import _summary_routing
    from lib.tasks_pkg.llm_fallback import _credential_recovery as recovery

    summary, dispatch, charged = summary_transport
    now = [0.0]
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    monkeypatch.setattr(_summary_routing, 'select_summary_route', lambda *a, **kw: {
        'modelId': 'same-model', 'providerId': 'same-provider'})
    calls = []
    content = '### Objective\nPreserve the same authorized summary.'
    usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'finish_reason': 'stop'}

    def respond(body, **kwargs):
        calls.append((deepcopy(body), kwargs['prefer_model'], get_pinned_provider()))
        if len(calls) < 3 or not recovers:
            cause = SubscriptionCredentialUnavailableError(
                reason='codex_command_timeout', credential_source='local', transient=True)
            raise LocalRequestPreparationError('credential temporarily unavailable') from cause
        if streaming:
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': content}, 'stop', usage)
        return content, usage

    monkeypatch.setattr(dispatch, 'dispatch_stream' if streaming else 'dispatch_chat', respond)
    task = {'_userId': 1, 'config': {'model': 'same-model'}}
    source = [{'role': 'user', 'content': 'Keep the original evidence.'}]
    original = deepcopy(source)
    with provider_pin('same-provider'):
        actual = summary._generate_query_aware_summary(
            source, 'continue', task=task, on_delta=(lambda delta: None) if streaming else None)
        assert get_pinned_provider() == 'same-provider'
    assert actual == (content if recovers else None)
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert calls[0][1:] == ('same-model', 'same-provider')
    assert len(charged) == int(recovers)
    assert source == original


def test_observed_summary_text_prevents_credential_retry(summary_transport, monkeypatch):
    from lib.llm.llm_errors import LocalRequestPreparationError, SubscriptionCredentialUnavailableError

    summary, dispatch, charged = summary_transport
    calls, deltas = [], []

    def fail_after_text(body, **kwargs):
        calls.append(body)
        kwargs['on_content']('Already generated text')
        cause = SubscriptionCredentialUnavailableError(
            reason='codex_command_timeout', credential_source='local', transient=True)
        raise LocalRequestPreparationError('misclassified callback failure') from cause

    monkeypatch.setattr(dispatch, 'dispatch_stream', fail_after_text)
    source = [{'role': 'user', 'content': 'Preserve source on partial output.'}]
    original = deepcopy(source)
    assert summary._generate_query_aware_summary(source, 'continue', on_delta=deltas.append) is None
    assert len(calls) == 1
    assert deltas == ['Already generated text']
    assert charged == []
    assert source == original
