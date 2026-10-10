"""Canonical swarm mirrors update the shipped app, not an iframe renderer.

The launch receipt and parent turn use the isolated authority supplied by
conftest. Only worker scheduling is held: no model or tool actually executes.
After launch the parent sink is detached, as for a retained background swarm;
new tool evidence can therefore reach the main panel only through the real
swarm mirror, PushHub, websocket, and native transient presentation overlay.
"""
from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


@pytest.fixture
def stored_swarm(flask_app, monkeypatch):
    from lib import turn_lifecycle as lifecycle
    from lib.conversations.repository import ConversationRepository
    from lib.storage import get_storage_client
    from lib.swarm.integration import _remove_session, execute_swarm_tool
    from lib.swarm.master import MasterOrchestrator
    from lib.tasks_pkg.manager import append_event, create_task, discard_task

    # Keep the real launch/session/receipt and mirror, without scheduling an
    # executor. Status recovery has no tool history to manufacture this row.
    scheduled = []
    monkeypatch.setattr(
        MasterOrchestrator, 'run_in_background', lambda master: scheduled.append(master))
    conv_id = f'e2e-swarm-routing-{uuid4().hex[:12]}'
    agent_id = f'routing-agent-{uuid4().hex[:8]}'
    owner_id = 1  # The isolated native-app fixture's authenticated owner.
    client = get_storage_client(write=True)
    task = None
    parent_detached = False
    try:
        created = lifecycle.create_turn_pair(
            conv_id, command_id=f'{conv_id}:create', user_id=owner_id,
            input_projection={'content': 'Observe isolated mirrored swarm evidence'},
            config={},
            conversation_defaults={
                'allowCreate': True, 'title': 'Swarm routing browser regression',
                'settings': {},
            })
        turn_id = created['turn']['turnId']
        attempt_id = created['attempt']['attemptId']
        task = create_task(
            conv_id, [], {
                'model': 'fixture-model', '_turnId': turn_id,
                '_attemptId': attempt_id,
            }, user_id=owner_id, supersede=False)
        assert task['id'] != conv_id
        assert lifecycle.claim_attempt_start(attempt_id, user_id=owner_id)
        lifecycle.bind_task(attempt_id, task['id'], user_id=owner_id)
        lifecycle.mark_task_started(attempt_id, task['id'], user_id=owner_id)
        task['status'] = 'running'
        args = {'agents': [{
            'id': agent_id, 'role': 'researcher',
            'objective': 'Inspect isolated routing evidence', 'max_retries': 0,
        }]}
        launch = {
            'toolName': 'spawn_agents', 'toolCallId': f'{conv_id}:spawn',
            'roundNum': 1, 'llmRound': 0, 'status': 'searching',
            'query': 'Inspect isolated routing evidence', 'toolArgs': args,
        }
        task['toolRounds'] = [launch]
        append_event(task, {'type': 'tool_start', **launch})

        def parent_sink(event):
            if parent_detached:
                raise RuntimeError('Synthetic parent sink detached after durable launch')
            # Task/replay sinks own their stamped copy, not the producer event.
            append_event(task, copy.deepcopy(event))

        receipt_text = execute_swarm_tool(
            'spawn_agents', args, task=task, on_event=parent_sink)
        receipt = json.loads(receipt_text)
        assert receipt['status'] == 'async_launched', receipt
        assert receipt['swarm_key'] == conv_id
        assert receipt['swarm_run_id'] == task['id']
        assert [agent['id'] for agent in receipt['agents']] == [agent_id]
        assert len(scheduled) == 1
        master = scheduled[0]
        launch.update(status='done', toolContent=receipt_text)
        append_event(task, {
            'type': 'tool_complete', 'toolName': 'spawn_agents',
            'toolCallId': launch['toolCallId'], 'roundNum': 1, 'llmRound': 0,
            'toolContent': receipt_text,
        })
        stored_turn = lifecycle.get_turn(conv_id, turn_id, user_id=owner_id)
        stored_round = stored_turn['projection']['toolRounds'][0]
        assert json.loads(stored_round['toolContent']) == receipt
        assert stored_round['taskId'] == task['id']
        assert stored_round['attemptId'] == attempt_id
        assert ConversationRepository().get(conv_id, user_id=owner_id) is not None
        assert master.get_status(include_tool_timeline=True)[agent_id]['toolCalls'] == []
        parent_detached = True
        yield {
            'convId': conv_id, 'turnId': turn_id, 'attemptId': attempt_id,
            'taskId': task['id'], 'swarmId': receipt['swarm_run_id'],
            'agentId': agent_id, 'master': master, 'ownerId': owner_id,
            'projection': copy.deepcopy(stored_turn['projection']),
        }
    finally:
        _remove_session(conv_id)
        if task is not None:
            discard_task(task['id'])
        client.command('conversation.delete', {
            'conv_id': conv_id, 'user_id': owner_id,
        }, f'{conv_id}:cleanup')


def test_canonical_mirrored_frame_updates_native_swarm_tool_row(
        stored_swarm, page, live_server, assert_no_js_errors, tmp_path):
    from lib import turn_lifecycle as lifecycle
    from lib.agent_core.push import push_event
    from tests.conftest import _dismiss_onboarding_modals

    received = []
    sent = []

    def record_frame(target, payload):
        try:
            frame = json.loads(payload)
        except (TypeError, ValueError):
            return
        if isinstance(frame, dict):
            target.append(frame)

    def observe_socket(socket):
        socket.on('framereceived', lambda payload: record_frame(received, payload))
        socket.on('framesent', lambda payload: record_frame(sent, payload))

    page.on('websocket', observe_socket)
    page.add_init_script("""
      window.swarmTestAppReady = false;
      window.addEventListener('tofu:app-ready', () => { window.swarmTestAppReady = true; });
    """)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_function('window.swarmTestAppReady === true')
    _dismiss_onboarding_modals(page)
    # Registry installation precedes catalog hydration. Navigate through the
    # actual sidebar only after this freshly stored conversation is present.
    catalog = page.request.get(f'{live_server}/api/v4/conversations?meta=1')
    assert catalog.status == 200, f'Native catalog HTTP {catalog.status}'
    catalog_body = catalog.json()
    catalog_data = catalog_body.get('data', catalog_body)
    catalog_ids = [item['id'] for item in catalog_data.get('items', [])]
    assert stored_swarm['convId'] in catalog_ids, f'Native catalog IDs: {catalog_ids}'

    catalog_row = next(item for item in catalog_data['items'] if item['id'] == stored_swarm['convId'])
    assert int(catalog_row['msgCount']) > 0, f'Native catalog turn count: {catalog_row}'
    sidebar_item = page.locator(f'.conv-item[data-conv-id="{stored_swarm["convId"]}"]')
    expect(sidebar_item).to_be_visible(timeout=15_000)
    sidebar_item.locator('.conv-title').click(position={'x': 8, 'y': 8})
    expect(page.get_by_role('dialog')).to_have_count(0)
    turn = page.locator(f'[data-turn-id="{stored_swarm["turnId"]}"]')
    card = turn.locator(f'.sw-agent[data-agent-id="{stored_swarm["agentId"]}"]')
    expect(card).to_be_visible(timeout=15_000)
    expect(card.locator('.sw-tl-row')).to_have_count(0)
    assert any(frame.get('action') == 'subscribe' and frame.get('channel') == 'swarm'
               for frame in sent), 'Native app did not subscribe to the real swarm websocket'

    producer = {
        'type': 'swarm_agent_tool_call',
        'conversationId': stored_swarm['convId'],
        'taskId': stored_swarm['taskId'],  # Execution ID, never a conversation ID.
        'turnId': stored_swarm['turnId'], 'attemptId': stored_swarm['attemptId'],
        'swarmId': stored_swarm['swarmId'], 'agentId': stored_swarm['agentId'],
        'callId': 'canonical-live-call', 'callStatus': 'done', 'toolName': 'read_files',
        'content': 'Read isolated routing evidence', 'status': 'running', 'phase': 'tool_use',
        'roundNum': 1, 'argsBrief': 'canonical-mirrored-evidence.txt',
    }
    foreign_attempt = {
        **producer, 'attemptId': f'foreign-attempt-{uuid4().hex}',
        'callId': 'foreign-attempt-call', 'argsBrief': 'foreign-attempt-evidence.txt',
    }
    foreign_run = {
        **producer, 'swarmId': f'foreign-run-{uuid4().hex}',
        'callId': 'foreign-run-call', 'argsBrief': 'foreign-run-evidence.txt',
    }
    # The real integration callback invokes the detached parent and then the
    # serving PushHub. A stale run is published through that same hub directly:
    # the current master's mirror correctly stamps its own run identity.
    # FIFO delivery makes the canonical row a barrier for both foreign frames;
    # the negative assertions therefore need no arbitrary sleep.
    for event in (foreign_attempt, foreign_run, producer):
        original = copy.deepcopy(event)
        if event is foreign_run:
            push_event('swarm', stored_swarm['convId'], event,
                       user_id=stored_swarm['ownerId'])
        else:
            stored_swarm['master'].on_progress(event)
        assert event == original, 'Mirroring mutated the producer event'
        assert event['taskId'] == stored_swarm['taskId']
        assert 'executionTaskId' not in event and 'convId' not in event

    row = card.locator('.sw-tl-row[data-call-id="canonical-live-call"]')
    expect(row).to_be_visible(timeout=15_000)
    expect(row.locator('.sw-tl-name')).to_have_text('read_files')
    expect(row.locator('.sw-tl-args')).to_have_text('canonical-mirrored-evidence.txt')
    expect(row).to_have_class('sw-tl-row sw-tl-done')
    expect(card.locator('.sw-tl-row')).to_have_count(1)
    expect(turn.locator('[data-call-id="foreign-attempt-call"]')).to_have_count(0)
    expect(turn.locator('[data-call-id="foreign-run-call"]')).to_have_count(0)
    expect(turn).not_to_contain_text('foreign-attempt-evidence.txt')
    expect(turn).not_to_contain_text('foreign-run-evidence.txt')

    frames = [frame for frame in received
              if frame.get('type') == producer['type'] and frame.get('channel') == 'swarm']
    calls = {frame.get('callId'): frame for frame in frames}
    assert set(calls) == {'foreign-attempt-call', 'foreign-run-call', 'canonical-live-call'}
    wire = calls['canonical-live-call']
    assert wire['conversationId'] == stored_swarm['convId']
    assert wire['taskId'] == stored_swarm['convId']
    assert wire['executionTaskId'] == producer['taskId']
    assert wire['attemptId'] == producer['attemptId']
    assert wire['swarmId'] == producer['swarmId']
    assert 'convId' not in wire and '_ownerUserId' not in wire
    assert calls['foreign-attempt-call']['attemptId'] == foreign_attempt['attemptId']
    assert calls['foreign-run-call']['swarmId'] == foreign_run['swarmId']

    # Neither storage hydration nor the status reconciler can supply this new
    # evidence: it exists only in the actual main-frame presentation overlay.
    durable = lifecycle.get_turn(
        stored_swarm['convId'], stored_swarm['turnId'], user_id=stored_swarm['ownerId'])
    assert durable['projection'] == stored_swarm['projection']
    status = stored_swarm['master'].get_status(include_tool_timeline=True)
    assert status[stored_swarm['agentId']]['toolCalls'] == []
    screenshot = tmp_path / 'native-sw-panel-canonical-mirror.png'
    page.screenshot(path=str(screenshot))
    print(f'Native swarm routing screenshot: {screenshot}')
