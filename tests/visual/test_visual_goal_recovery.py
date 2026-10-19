"""Restart recovery through isolated storage, real HTTP and the shipped browser UI.

Only replacement model execution is held at its start port. Admission, Goal
ownership, Turn projection and request normalization use production owners.
"""
from __future__ import annotations

from dataclasses import replace
import threading
from uuid import uuid4

import pytest

pytestmark = pytest.mark.visual


@pytest.fixture
def interrupted_goal(page, monkeypatch, request):
    from lib import turn_lifecycle as lifecycle
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.goal_runs.recovery import GoalRecoveryPorts
    from lib.goal_runs.service import GoalRunService
    from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
    from lib.orchestration.chat_live_turn import begin_live_flow_turn
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
    from lib.storage import get_storage_client

    conv_id = f'visual-goal-recovery-{uuid4().hex[:12]}'
    config = {'autopilot': True, 'model': 'fixture-model', '_goalObjective': 'Preserve verified progress'}
    pair = lifecycle.create_turn_pair(conv_id, command_id=conv_id, user_id=1,
        input_projection={'role': 'user', 'content': config['_goalObjective']}, config=config,
        conversation_defaults={'allowCreate': True, 'title': 'Interrupted Goal recovery', 'settings': {}})
    task = {'id': uuid4().hex, 'convId': conv_id, '_userId': 1, 'config': config,
            '_turnId': pair['turn']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            'messages': [{'role': 'user', 'content': config['_goalObjective']}]}
    lifecycle.bind_task(task['_attemptId'], task['id'], user_id=1)
    definition = {'schema': 'tofu.orchestration/v1', 'name': 'Recovery browser fixture',
        'nodes': [{'id': 's', 'type': 'control', 'kind': 'start'},
                  {'id': 'w', 'type': 'role', 'role': 'general', 'params': {}},
                  {'id': 'z', 'type': 'control', 'kind': 'stop'}],
        'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'z'}]}
    service = GoalRunService()
    service.start(task, definition)
    launch = OrchestrationChatFlowLaunchSpec(projection='autopilot', initial_phase='worker',
        initial_context='original context', system_prompt_base='fixed policy', tools=(),
        model='fixture-model', project_path='', thinking_enabled=None, max_iterations=40)
    journal = create_goal_execution_journal(task, definition, launch)
    begin_live_flow_turn(task, {'flowProjection': 'autopilot', 'turnRole': 'worker', 'iteration': 1})
    # This is the first worker: no verifier marker exists to infer a Goal from.
    before = lifecycle.get_turn(conv_id, task['_turnId'], user_id=1)
    assert before['projection']['orchestration']['goalRunId'] == task['_goalRunId']
    assert not before['projection'].get('_autopilotRunId')
    if getattr(request, 'param', '') == 'unknown_effect':
        class Crash(BaseException):
            pass
        with pytest.raises(Crash):
            journal.runner(lambda *args: (_ for _ in ()).throw(Crash()),
                context_snapshot=lambda: {}, context_restore=lambda state: None)(
                    definition['nodes'][1], launch.initial_context, 0)
    lifecycle.recover_running_attempts()
    retire_interrupted_orchestration_runs_detailed(
        error={'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'})
    after = lifecycle.get_turn(conv_id, task['_turnId'], user_id=1)
    assert after['projection']['orchestration']['goalRunId'] == task['_goalRunId']
    started = []
    ports = GoalRecoveryPorts.defaults(1, '')
    def start(conv, cfg, data, abort_after, registered, **kwargs):
        successor = {'id': uuid4().hex, 'convId': conv, '_userId': 1, 'config': cfg,
            '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
            'messages': task['messages'], 'abort_event': threading.Event()}
        registered(successor['id'])
        service.start(successor, definition)
        create_goal_execution_journal(successor, definition, launch)
        started.append(successor)
        return successor['id'], None
    commands = ConversationTurnCommandService(build_user_message=lambda *args: None,
        was_aborted_after=lambda *args: False, start_task=start)
    monkeypatch.setattr(GoalRecoveryPorts, 'defaults', classmethod(
        lambda cls, owner, tenant: replace(ports, commands=commands)))
    try:
        yield {'conversation': conv_id, 'turn': task['_turnId'], 'run': task['_goalRunId'], 'started': started}
    finally:
        # The replacement model is held at its start port in this fixture.
        # Finish that lifecycle before deleting, like production abort-before-delete.
        for successor in started:
            lifecycle.abort_attempt(successor['_attemptId'], user_id=1)
        get_storage_client(write=True).command('conversation.delete',
            {'conv_id': conv_id, 'user_id': 1}, f'{conv_id}:cleanup')


def load_goal(page, goal):
    from tests.conftest import _dismiss_onboarding_modals
    page.reload(wait_until='domcontentloaded')
    _dismiss_onboarding_modals(page)
    page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
    page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", goal['conversation'])
    notice = page.locator(f'[data-goal-recovery][data-ap-run-id="{goal["run"]}"]')
    notice.wait_for()
    return notice


@pytest.mark.parametrize('width', [1100, 390])
def test_goal_first_worker_recovers_via_real_http_and_survives_reload(
        page, interrupted_goal, assert_no_js_errors, tmp_path, width):
    page.set_viewport_size({'width': width, 'height': 900})
    notice = load_goal(page, interrupted_goal)
    button = notice.locator('button')
    button.wait_for()
    assert notice.get_attribute('data-goal-recovery') == 'idle'
    page.screenshot(path=str(tmp_path / f'goal-recovery-{width}.png'))
    with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/resume')) as response:
        button.click()
    assert response.value.status == 200, response.value.text()
    result = response.value.json()['data']
    assert result['run']['id'] != interrupted_goal['run']
    assert len(interrupted_goal['started']) == 1
    # A fresh event may already replace the action with historical context.
    page.wait_for_function("id => document.querySelector(`[data-ap-run-id=\"${id}\"][data-goal-recovery=accepted]`) || !document.querySelector(`[data-ap-run-id=\"${id}\"][data-goal-recovery] button`)", arg=interrupted_goal['run'])
    # The server command is idempotent even across browser reload/retries.
    repeated = page.request.post(response.value.url)
    assert repeated.status == 200
    assert repeated.json()['data']['run']['id'] == result['run']['id']
    assert len(interrupted_goal['started']) == 1
    notice = load_goal(page, interrupted_goal)
    assert notice.locator('button').count() == 0
    assert page.locator('body').evaluate('(node) => node.scrollWidth <= innerWidth + 1')


@pytest.mark.parametrize('interrupted_goal', ['unknown_effect'], indirect=True)
def test_goal_unknown_effect_refuses_without_new_execution(
        page, interrupted_goal):
    notice = load_goal(page, interrupted_goal)
    with page.expect_response(lambda response: response.url.endswith('/resume')) as response:
        notice.locator('button').click()
    assert response.value.status == 409, response.value.text()
    page.wait_for_function("document.querySelector('[data-goal-recovery=refused]')")
    assert interrupted_goal['started'] == []
    load_goal(page, interrupted_goal).locator('button').wait_for()
    errors = getattr(page, '_tofu_js_errors', [])
    assert errors and all(error == 'console.error: Failed to load resource: the server responded with a status of 409 ()' for error in errors), errors


def test_goal_lost_http_ack_keeps_uncertainty_then_reload_shows_successor(
        page, interrupted_goal):
    notice = load_goal(page, interrupted_goal)
    requests = []
    def lose_ack(route):
        requests.append(route.request.url)
        response = route.fetch()
        assert response.status == 200, response.text()
        route.abort('failed')
    page.route('**/orchestrations/tasks/*/resume', lose_ack)
    notice.locator('button').click()
    page.wait_for_function("document.querySelector('[data-goal-recovery=unknown]') || (document.querySelector('[data-goal-recovery]') && !document.querySelector('[data-goal-recovery] button'))")
    assert len(requests) == 1
    assert len(interrupted_goal['started']) == 1
    page.unroute('**/orchestrations/tasks/*/resume', lose_ack)
    assert load_goal(page, interrupted_goal).locator('button').count() == 0
    expected = {'console.error: Failed to load resource: net::ERR_FAILED',
                f'request failed: {requests[0]} (net::ERR_FAILED)'}
    errors = getattr(page, '_tofu_js_errors', [])
    assert errors and all(error in expected for error in errors), errors
