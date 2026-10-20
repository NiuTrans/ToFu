"""Provider outage recovery through the real invocation, dispatch and settlement."""
import uuid

import pytest

from tests.llm.test_llm_replay_scenarios import (
    FIXTURES_ROOT, _assert_invariants_clean, _durable_events, _expected,
    _install_replay_route, _install_transport, _make_task,
)

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

@pytest.fixture(scope='module', autouse=True)
def release_replay_dispatcher():
    yield
    from lib.llm_dispatch import reset_dispatcher
    reset_dispatcher()


def _run(name, tmp_path, monkeypatch, *, disable_auto_retry=False):
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY

    _install_replay_route()
    task = _make_task('cv-upstream-' + uuid.uuid4().hex[:10],
                      'Reply with a brief completion acknowledgement.', None)
    task['config'].update(disableModelFallback=True, autoTurnRetryMax=1)
    if disable_auto_retry:
        task['config']['disableAutoTurnRetry'] = True
    # Exercise the existing configured exit from an inner outage wait. The
    # production default remains waitable until recovery or owner cancellation.
    monkeypatch.setenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', '0.001')
    monkeypatch.setattr('lib.tasks_pkg.turn_retry.auto_turn_backoff_seconds', lambda _: 0)
    _install_transport(FIXTURES_ROOT / name, monkeypatch, cwd=str(tmp_path),
                       record_fixture=not disable_auto_retry)
    execute_invocation(task, policy=ROOT_INVOCATION_POLICY)
    return task


def test_upstream_outage_recovers_in_the_real_invocation(tmp_path, monkeypatch):
    expected = _expected('upstream_turn_recovery')
    task = _run('upstream_turn_recovery', tmp_path, monkeypatch)
    assert task['status'] == expected['status'], task.get('error')
    assert task.get('error') is None
    assert task['content'] == expected['content']
    assert task.get('_auto_turn_retry_count') == expected['retry_count']
    events = _durable_events(task)
    assert sum(event['type'] == 'retry_reset' for event in events) == 1
    done = [event for event in events if event['type'] == 'done']
    assert len(done) == 1 and not done[0].get('error')
    _assert_invariants_clean(task)


def test_persistent_upstream_outage_exhausts_once_and_keeps_the_failure(tmp_path, monkeypatch):
    expected = _expected('upstream_turn_exhaustion')
    task = _run('upstream_turn_exhaustion', tmp_path, monkeypatch)
    assert task['status'] == expected['status']
    assert task['error']['kind'] == expected['error_kind']
    assert task['error']['statusCode'] == expected['status_code']
    assert task.get('_auto_turn_retry_count') == expected['retry_count']
    events = _durable_events(task)
    assert sum(event['type'] == 'retry_reset' for event in events) == 1
    done = [event for event in events if event['type'] == 'done']
    assert len(done) == 1 and done[0]['error']['kind'] == expected['error_kind']
    _assert_invariants_clean(task)


def test_explicit_retry_opt_out_still_closes_the_failed_model_round(tmp_path, monkeypatch):
    task = _run('upstream_turn_exhaustion', tmp_path, monkeypatch, disable_auto_retry=True)
    assert task['status'] == 'error'
    assert task['error']['kind'] == 'upstream_error'
    assert not task.get('_auto_turn_retry_count')
    events = _durable_events(task)
    assert not any(event['type'] == 'retry_reset' for event in events)
    assert sum(event['type'] == 'done' for event in events) == 1
    _assert_invariants_clean(task)
