"""Goal history through real Sidecar, v3 HTTP, shipped UI and Inspector routes.

Only model/tool work is scripted. The app fixture owns isolated storage and an
HTTP server; this test never writes user data or replaces browser reducers/API.
"""
from __future__ import annotations

import copy
from uuid import uuid4

import pytest

pytestmark = pytest.mark.visual


@pytest.fixture
def persisted_goal(page, monkeypatch):
    import lib
    from lib import turn_lifecycle as lifecycle
    from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
    from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink
    from lib.orchestration.chat_flow_adapter import FlowEventAdapter
    from lib.orchestration.chat_turn_persistence import OrchestrationChatTurnPersistence
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import append_event, create_task, discard_task
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    from lib.tasks_pkg.orchestrator._finalize import _discard_pretool_prose
    import lib.tasks_pkg.child_invocation as child_owner

    monkeypatch.setattr(lib, 'DEBUG_MODE', True)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MIN_INTERVAL_S', '0')
    conv_id = f'visual-goal-{uuid4().hex[:12]}'
    created = lifecycle.create_turn_pair(
        conv_id, command_id=conv_id, user_id=1,
        input_projection={'content': 'Verify the synthetic Goal evidence'}, config={},
        conversation_defaults={'allowCreate': True, 'title': 'Goal durable browser evidence',
                               'createdAt': 1, 'settings': {}})
    task = create_task(conv_id, [], {'model': 'fixture-model',
        '_turnId': created['turn']['turnId'], '_attemptId': created['attempt']['attemptId']},
        user_id=1, supersede=False)
    client = get_storage_client(write=True)
    client.command('turn.attempt.bind', {'attempt_id': task['_attemptId'],
        'user_id': 1, 'task_id': task['id']}, f'{conv_id}:bind')
    sink = OrchestrationChatTaskEventSink(task, append_event)
    persistence = OrchestrationChatTurnPersistence(task,
        store_turns=lambda owner, turns: owner.update(_flow_turns=turns),
        sync_turns=lifecycle.sync_visible_run_turns)
    adapter = FlowEventAdapter(emit=persistence, on_stream=sink, projection='autopilot')
    persistence.bind(adapter.messages)
    refs = []
    try:
        adapter.on_event({'type': 'step_start', 'role': 'worker'})
        adapter.on_event({'type': 'step_delta', 'role': 'worker', 'kind': 'content',
                          'chunk': 'Opening worker evidence'})
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'output': 'Opening worker evidence', 'status': 'completed'})
        adapter.on_event({'type': 'step_start', 'role': 'virtual_user', 'emits': 'user'})
        verifier_turn = task['_flowLiveTurn']['turnId']

        def scripted(child, *, policy):
            ref = begin_round_evidence(child, child['_inspectorTaskId'], 1, 'verification')
            refs.append(ref)
            append_event(child, build_round_evidence(child, [], ref=ref, kind='request',
                model='fixture-model', prepared_messages=[{'role': 'user', 'content': 'Exact Goal request'}]))
            child.update(content='Checking durable files', thinking='Preserve historical reasoning')
            append_event(child, {'type': 'delta', 'content': child['content'], 'thinking': child['thinking']})
            row = {'toolName': 'read_files', 'toolCallId': 'verify-one', 'roundNum': 1,
                   'llmRound': 0, 'taskId': child['id'], 'query': 'Verify durable fixture',
                   'toolArgs': {'path': 'synthetic.txt'}, 'status': 'searching',
                   'assistantContent': child['content'], 'thinking': child['thinking'], 'inspectorRef': ref}
            child['toolRounds'] = [row]
            append_event(child, {'type': 'tool_start', **{key: value for key, value in row.items()
                         if key not in ('assistantContent', 'thinking')}})
            _discard_pretool_prose(child, 0)
            row.update(status='done', toolContent='Exact durable tool evidence')
            append_event(child, {'type': 'tool_complete', 'roundNum': 1, 'toolCallId': 'verify-one',
                'toolName': 'read_files', 'toolContent': row['toolContent'], 'status': 'done', 'inspectorRef': ref})
            append_event(child, build_round_evidence(child, [], ref=ref, kind='state',
                model='fixture-model', prepared_messages=[{'role': 'tool', 'tool_call_id': 'verify-one',
                                                           'content': row['toolContent']}]))
            child.update(content='Verification finished with full evidence', thinking='',
                         status='done', finishReason='stop', streamState='provider_finished')
            append_event(child, {'type': 'delta', 'content': child['content']})
            from lib.tasks_pkg.segments import assemble_segments
            child['segments'] = assemble_segments(child)
            append_event(child, {'type': 'done', 'finishReason': 'stop', 'usage': {}})

        monkeypatch.setattr(child_owner, 'execute_invocation', scripted)
        result = OrchestrationInvocationRunner(OrchestrationAgentRunnerConfig(
            parent_task=task, model='fixture-model', model_routing_policy='selected'),
            emit=adapter.on_event, abort_check=lambda: False)(
                {'id': 'verification', 'role': 'virtual_user', 'params': {'emits': 'user'}}, '', 1)
        assert result.status == 'completed'
        adapter.on_event({'type': 'step_complete', 'role': 'virtual_user', 'emits': 'user',
            'status': result.status, 'outputProjection': result.output_projection})
        projection = lifecycle.get_turn(conv_id, verifier_turn, user_id=1)['projection']

        assert len(projection['toolRounds']) == 1
        assert len([b for b in projection['segments'] if b['type'] == 'tool_use']) == 1
        assert projection['toolRounds'][0]['inspectorRef'] == refs[0]
        # Evict the live executor: HTTP must authorize/read cold persisted data.
        discard_task(task['id'])
        yield {'convId': conv_id, 'turnId': verifier_turn, 'ref': refs[0],
               'projection': copy.deepcopy(projection)}
    finally:
        discard_task(task['id'])
        client.command('conversation.delete', {'conv_id': conv_id, 'user_id': 1},
                       f'{conv_id}:cleanup')


@pytest.mark.parametrize('width', [1100, 390])
def test_goal_persisted_history_and_inspector_http_reload(
        page, persisted_goal, assert_no_js_errors, tmp_path, width):
    from tests.conftest import _dismiss_onboarding_modals
    from urllib.parse import quote, urljoin

    page.set_viewport_size({'width': width, 'height': 900})
    ref = persisted_goal['ref']
    url = f'/api/v4/tasks/{quote(ref["taskId"], safe="")}/requests/1?kind=state&turn={quote(ref["turn"], safe="")}'
    response = page.request.get(urljoin(page.url, url))
    assert response.status == 200, response.text()
    assert response.json()['messages'][0]['content'] == 'Exact durable tool evidence'
    page.reload(wait_until='domcontentloaded')
    _dismiss_onboarding_modals(page)
    page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
    page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", persisted_goal['convId'])
    turn = page.locator(f'[data-turn-id="{persisted_goal["turnId"]}"]')
    turn.get_by_text('Checking durable files', exact=True).wait_for()
    ids = turn.locator('[data-block-id]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.blockId)')
    for cold in (False, True):
        if cold:
            page.reload(wait_until='domcontentloaded')
            _dismiss_onboarding_modals(page)
            turn.get_by_text('Checking durable files', exact=True).wait_for()
            assert turn.locator('[data-block-id]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.blockId)') == ids
        assert 'Verification finished with full evidence' in turn.inner_text()
        thinking = turn.locator('.thinking-block')
        if not thinking.evaluate('(node)=>node.open'):
            thinking.locator('summary').click()
        assert 'Preserve historical reasoning' in thinking.inner_text()
        anchor = turn.locator('.ri-tool-anchor')

        assert anchor.count() == 1  # One execution, not one row per checkpoint/settlement.
        assert anchor.get_attribute('data-ri-turn') == ref['turn']
        with page.expect_response(lambda resp: '/requests/1?' in resp.url and 'kind=state' in resp.url) as read:
            anchor.click()
        assert read.value.status == 200
        panel = page.locator('.ri-state-panel')
        # Inspector also exposes a quoted JSON view of the same payload.
        panel.get_by_text('Exact durable tool evidence', exact=True).wait_for()
        assert panel.get_attribute('data-ri-kind') == 'state'
        assert page.locator('body').evaluate('(node)=>node.scrollWidth<=innerWidth+1')
    screenshot = tmp_path / f'goal-persisted-{width}.png'
    page.screenshot(path=str(screenshot))
    print(f'Persisted Goal browser screenshot: {screenshot}')
