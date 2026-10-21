"""HTTP client failures retain effect authority through actual tool settlement."""
import ssl

import httpx
import pytest
import requests
from urllib3.exceptions import ReadTimeoutError

from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit

CONNECTION_ERRORS = [
    httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.CloseError,
    httpx.RemoteProtocolError, httpx.ProxyError, requests.exceptions.ConnectionError,
    requests.exceptions.ProxyError, requests.exceptions.ChunkedEncodingError,
]
TIMEOUT_ERRORS = [
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
    requests.exceptions.Timeout, requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
]


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('failure_type', CONNECTION_ERRORS + TIMEOUT_ERRORS)
def test_http_error_reaches_model_as_typed_recovery(mode, failure_type, observations):
    calls = []

    def handler(_task, _tc, _name, call_id, *args):
        calls.append(call_id)
        raise failure_type('interrupted source')

    task = recovery_task(handler, 'read_only', mode)
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    expected = 'tool_execution_timeout' if failure_type in TIMEOUT_ERRORS else 'tool_connection_failed'
    assert error.code == expected and error.retryable
    assert failure_type.__name__ in error.message and 'interrupted source' in error.message
    assert 'gateway' in error.next_action and 'independent' in error.next_action
    assert row['status'] == 'error' and settled[0].execution_status == 'error'
    assert row['executionEffect']['state'] == 'unknown'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert calls == ['occurrence-1'] and not task.get('_activeToolContexts')


@pytest.mark.parametrize('policy', ['read_only', 'idempotent', 'reconcile', 'missing', 'contradictory'])
@pytest.mark.parametrize('failure_type', [httpx.ReadError, httpx.ReadTimeout,
    requests.exceptions.ConnectionError, requests.exceptions.Timeout])
def test_http_recovery_requires_current_and_captured_effect_authority(
        policy, failure_type, observations):
    def handler(*args):
        raise failure_type('connection interrupted after sending request')

    row, messages, _ = dispatch(recovery_task(handler, policy))
    error = tool_result_error(messages[0]['content'])
    assert error.retryable is (policy in {'read_only', 'idempotent'})
    if not error.retryable:
        assert error.code == 'tool_effect_unknown'
        assert row['executionEffect']['operationId'] in error.message
        assert 'Do not repeat' in error.next_action


@pytest.mark.parametrize('failure_type', [httpx.ReadError, requests.exceptions.ReadTimeout])
def test_changed_contract_cannot_grant_http_replay(failure_type, observations):
    def handler(task, *args):
        document = task['_toolContractDocumentsByName']['fetch_url']
        document['effectRecovery'] = document['idempotency'] = 'idempotent'
        raise failure_type('response missing')

    row, messages, _ = dispatch(recovery_task(handler, 'read_only'))
    error = tool_result_error(messages[0]['content'])
    assert error.code == 'tool_effect_unknown' and not error.retryable
    assert row['executionEffect']['recovery'] == 'read_only'


def classified(failure):
    from lib.tasks_pkg.executor._effects import tool_exception_result
    return tool_result_error(tool_exception_result(
        recovery_task(None, 'read_only'), 'fetch_url', {}, failure))


@pytest.mark.parametrize('failure', [
    httpx.LocalProtocolError('invalid request'),
    httpx.UnsupportedProtocol('wrong scheme'), httpx.InvalidURL('invalid url'),
    httpx.TooManyRedirects('redirect loop'), httpx.DecodingError('invalid payload'),
    httpx.HTTPStatusError('503 timeout', request=httpx.Request('GET', 'https://example.test'),
                          response=httpx.Response(403)),
    requests.exceptions.InvalidURL('invalid url'),
    requests.exceptions.TooManyRedirects('redirect loop'),
    requests.exceptions.ContentDecodingError('invalid payload'),
    requests.exceptions.HTTPError('503 timeout'),
    requests.exceptions.RequestException('connection lost'),
    RuntimeError('requests.ConnectionError: timeout'),
    type('ConnectError', (Exception,), {'__module__': 'httpx'})('connection lost'),
])
def test_http_status_configuration_payload_and_prose_do_not_grant_retry(failure):
    error = classified(failure)
    assert error.code == 'tool_execution_failed' and not error.retryable
    assert 'repair' in error.next_action


@pytest.mark.parametrize('link', ['__cause__', '__context__', 'args', 'reason'])
def test_nested_tls_failure_is_not_advertised_as_transient(link):
    failure = httpx.ConnectError('handshake failed')
    wrapper = RuntimeError('transport wrapper')
    tls = ssl.SSLCertVerificationError('certificate rejected')
    wrapper.__cause__ = tls
    if link == 'args':
        failure.args = (wrapper,)
    else:
        setattr(failure, link, wrapper)
    error = classified(failure)
    assert error.code == 'tool_execution_failed' and not error.retryable


@pytest.mark.parametrize('failure_type', [requests.exceptions.SSLError, ssl.SSLError])
def test_tls_failure_requires_repair(failure_type):
    error = classified(failure_type('handshake failed'))
    assert error.code == 'tool_execution_failed' and not error.retryable


def test_timeout_during_tls_read_keeps_typed_timeout():
    failure = httpx.ReadTimeout('response timed out')
    failure.__context__ = ssl.SSLWantReadError('more encrypted bytes needed')
    error = classified(failure)
    assert error.code == 'tool_execution_timeout' and error.retryable


def test_requests_streaming_timeout_keeps_wrapped_typed_evidence():
    failure = requests.exceptions.ConnectionError(ReadTimeoutError(None, '/resource', 'read timeout'))
    error = classified(failure)
    assert error.code == 'tool_execution_timeout' and error.retryable


def test_application_wrapper_does_not_inherit_http_retry():
    failure = RuntimeError('application failed after network recovery')
    failure.__cause__ = httpx.ConnectError('connection lost')
    error = classified(failure)
    assert error.code == 'tool_execution_failed' and not error.retryable


def test_owner_cancellation_takes_precedence_over_http_timeout():
    from concurrent.futures import CancelledError

    class CancelledHttpRead(CancelledError, httpx.ReadTimeout):
        pass

    error = classified(CancelledHttpRead('owner cancelled the read'))
    assert error.code == 'tool_execution_cancelled' and not error.retryable
    assert 'Respect the cancellation' in error.next_action
    assert 'Continue' not in error.next_action


def test_real_http_subclass_and_cyclic_causes_are_supported():
    class CustomReadError(httpx.ReadError):
        pass

    failure = CustomReadError('connection lost')
    failure.__cause__ = RuntimeError('wrapper')
    failure.__cause__.__context__ = failure
    error = classified(failure)
    assert error.code == 'tool_connection_failed' and error.retryable


def test_uninspected_exception_graph_does_not_claim_transient_recovery():
    failure = httpx.ConnectError('handshake failure')
    tail = failure
    for _ in range(64):
        tail.__cause__ = RuntimeError('nested transport context')
        tail = tail.__cause__
    tail.__cause__ = ssl.SSLCertVerificationError('certificate rejected')
    error = classified(failure)
    assert error.code == 'tool_execution_failed' and not error.retryable
