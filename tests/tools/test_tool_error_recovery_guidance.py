"""Fallback guidance preserves failures without inventing stop or replay authority."""
import pytest

from lib.tools.result_envelope import tool_result_error, typed_tool_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('retryable', [True, False])
@pytest.mark.parametrize('hint', ['', '  \n '])
def test_missing_hint_preserves_error_and_scopes_recovery(retryable, hint):
    result = typed_tool_error('owner_failure', retryable=retryable,
                              next_action=hint, message='original cause')
    error = tool_result_error(result.to_model_text())
    assert error.code == 'owner_failure'
    assert error.retryable is retryable
    assert error.message == 'original cause'
    assert 'verify its outcome' in error.next_action
    assert 'Continue independent work' in error.next_action
    assert len(error.next_action) <= 300


def test_owner_specific_recovery_is_preserved():
    hint = 'Wait for the current operation receipt. Do not submit another write.'
    error = typed_tool_error('operation_pending', retryable=False,
                             next_action=hint).error
    assert error.next_action == hint


def test_download_fallback_uses_common_recovery_without_task_stop():
    from lib.tasks_pkg.handlers.search._core import _download_failure
    result = _download_failure('download_failed', 'original cause',
                               retryable=False, next_action='')
    expected = typed_tool_error('download_failed', retryable=False,
                                next_action='').error.next_action
    assert result['next_action'] == expected
    assert result['location'] is None and result['retryable'] is False


@pytest.mark.parametrize('http_status', [None, 500])
def test_webhook_lost_result_does_not_suggest_replaying(monkeypatch, http_status):
    from types import SimpleNamespace
    from lib.tools.tool_env import mint_tool_env, dispose_tool_env
    calls = []

    def post(*args, **kwargs):
        calls.append('effect applied')
        if http_status is None:
            raise TimeoutError('receipt lost')
        return SimpleNamespace(status_code=http_status, text='receipt failed')

    monkeypatch.setattr('lib.byo.byo_egress.validate_egress_url', lambda *_: None)
    monkeypatch.setattr('lib.http_transport.client.http_post', post)
    env = mint_tool_env(tools=[{
        'type': 'function', 'function': {
            'name': 'custom__write', 'description': 'test',
            'parameters': {'type': 'object', 'properties': {}}},
        'write': True, 'execution': {'mode': 'webhook', 'url': 'https://example.com/tool'},
    }])
    try:
        content, failed = env._run_webhook(env.tools[0], {})
        assert failed and calls == ['effect applied']
        assert ('receipt lost' if http_status is None else 'HTTP 500') in content
        assert 'may have taken effect' in content
        assert 'Do not automatically repeat' in content
    finally:
        dispose_tool_env(env)
