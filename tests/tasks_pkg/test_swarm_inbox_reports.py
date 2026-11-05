"""Completion delivery keeps report XML intact in model and display projections."""
import pytest

from lib.agent_inbox import format_swarm_update
from lib.tasks_pkg.orchestrator import _swarm_inbox
from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
from lib.turn_lifecycle import capture_task_projection_source, _task_projection

pytestmark = pytest.mark.unit


def test_completion_report_survives_event_and_reload_projection(monkeypatch):
    report = 'REPORT HEAD\n' + ('中文报告 <evidence> & verified\n' * 300) + 'REPORT TAIL'
    payload = format_swarm_update(
        agent_id='reviewer', swarm_run_id='run-1', role='general', status='completed',
        elapsed_seconds=2285.2, tokens=2051923, result=report,
        output_file='/synthetic/reviewer.log')
    item = {'agent_id': 'reviewer', 'value': payload}
    task = {'id': 'synthetic-task', 'convId': 'synthetic-conversation'}
    messages = []
    events, sources = [], []

    def capture(task, event):
        events.append(event)
        sources.append(capture_task_projection_source(task, event))

    def drain(_key, **kwargs):
        return [item] if 'exclude_modes' in kwargs else []

    monkeypatch.setattr('lib.agent_inbox.drain', drain)
    monkeypatch.setattr(_swarm_inbox, 'append_event', capture)
    _swarm_inbox.drain_and_inject_inbox(
        task=task, messages=messages, round_num=2, tid='synthetic')

    assert len(messages) == 1
    assert payload in messages[0]['content']
    assert task['_swarm_inject_pending'] == [item]
    previews = task['_inboxInjects'][0]['previews']
    assert previews == [{'agentId': 'reviewer', 'text': payload}]
    injection = next(event for event in events if event.get('type') == 'swarm_inbox_inject')
    assert injection['previews'] == previews

    assert len(sources) == 1
    assert sources[0]['_inboxInjects'][0]['previews'] == previews
    projection = _task_projection(sources[0], {}, events[0])
    assert projection['_inboxInjects'][0]['previews'] == previews
    assert not [segment for segment in projection['segments'] if segment['type'] == 'system_note']
    assert not task.get(INJECTED_NOTES_FIELD) and not task.get('toolRounds')
    manifest, = task['_contextManifest']
    assert manifest['displaySurface'] == 'sidecar'
    assert manifest['displayOwner'] == 'inbox-lanes'
    assert 'REPORT TAIL</result>' in previews[0]['text']
    # Report preservation must not manufacture provider-consumption acknowledgement.
    assert not task.get('_swarm_delivered_result_ids')


def test_completion_event_failure_keeps_sidecar_and_deferred_lane_receipts(monkeypatch):
    swarm = {'agent_id': 'worker', 'value': '<swarm-update>result</swarm-update>'}
    peer = {'queueId': 'peer-row', 'value': 'Peer evidence'}
    steer = {'queueId': 'steer-row', 'value': 'Human correction'}
    background = {'queueId': 'background-row', 'value': 'Command completed'}
    lanes = {'peer-msg': peer, 'user-steer': steer, 'background-command': background}
    task, messages, snapshots = {'id': 'task', 'convId': 'conversation'}, [], []

    def drain(_key, **kwargs):
        if 'exclude_modes' in kwargs:
            return [swarm]
        return [lanes[kwargs['modes'][0]]]

    def fail_event(task, event):
        snapshots.append(capture_task_projection_source(task, event))
        raise RuntimeError('injected observer failure')

    monkeypatch.setattr('lib.agent_inbox.drain', drain)
    monkeypatch.setattr(_swarm_inbox, 'append_event', fail_event)
    _swarm_inbox.drain_and_inject_inbox(task=task, messages=messages, round_num=2, tid='task')
    assert len(messages) == 1 and messages[0]['_containsHumanSteer'] is True
    assert task['_swarm_inject_pending'] == [swarm]
    assert task['_peer_inject_pending'] == [peer]
    assert task['_steer_inject_pending'] == [steer]
    assert task['_bgcmd_inject_pending'] == [background]
    sidecar, = task['_inboxInjects']
    assert sidecar['previews'] == [{'agentId': 'worker', 'text': swarm['value']}]
    assert snapshots[0]['_inboxInjects'] == task['_inboxInjects']
    projection = _task_projection(capture_task_projection_source(task, {'type': 'llm_round_start'}), {})
    assert projection['_inboxInjects'][0]['previews'] == sidecar['previews']
    assert not task.get(INJECTED_NOTES_FIELD) and not task.get('toolRounds')
    assert not task.get('_swarm_delivered_result_ids')
    assert not task.get('_peerInjects') and not task.get('_userSteerInjects')


def test_driver_peer_drain_explicitly_owns_deferred_display(monkeypatch):
    from lib.tasks_pkg.orchestrator._turn import drain_peer_messages_into

    item = {'queueId': 'peer-row', 'value': 'Peer evidence'}
    task, messages = {'id': 'driver', '_peer_drain_key': 'conversation'}, []
    monkeypatch.setattr('lib.agent_inbox.drain', lambda *a, **kw: [item])
    assert drain_peer_messages_into(task, messages, round_label=3) == 1
    assert task['_peer_inject_pending'] == [item]
    assert messages[0]['_containsHumanSteer'] is False
    assert not task.get(INJECTED_NOTES_FIELD) and not task.get('_peerInjects')
    row, = task['_contextManifest']
    assert row['displaySurface'] == 'sidecar' and row['displayOwner'] == 'peer-lane'


@pytest.mark.parametrize('with_completion', [False, True])
def test_task_local_advice_has_visible_note_not_false_sidecar_claim(monkeypatch, with_completion):
    advice = '[Project overlap advisory] synthetic shared path'
    completion = {'agent_id': 'worker', 'value': '<swarm-update>result</swarm-update>'}
    task = {'id': 'task', 'convId': 'conversation',
            '_projectOverlapAdvisories': [{'key': 'path-pair', 'value': advice}]}
    messages, captured = [], []
    monkeypatch.setattr('lib.agent_inbox.drain', lambda _key, **kw: (
        [completion] if with_completion and 'exclude_modes' in kw else []))
    monkeypatch.setattr(_swarm_inbox, 'append_event', lambda task, event: (
        captured.append(capture_task_projection_source(task, event))))
    _swarm_inbox.drain_and_inject_inbox(task=task, messages=messages, round_num=2, tid='task')
    assert len(messages) == (2 if with_completion else 1)
    assert messages[-1]['content'] == advice
    assert not messages[-1]['_containsHumanSteer']
    note, = task[INJECTED_NOTES_FIELD]
    assert note['text'] == advice and note['reason'] == 'inbox_delivery'
    assert task['_contextManifest'][-1]['displaySurface'] == 'system_note'
    assert '_projectOverlapAdvisories' not in task
    source = captured[0] if captured else capture_task_projection_source(task, {'type': 'llm_round_start'})
    projection = _task_projection(source, {})
    projected, = [segment for segment in projection['segments'] if segment['type'] == 'system_note']
    assert projected['text'] == advice
    assert not task.get('toolRounds') and not task.get('_swarm_delivered_result_ids')
    if with_completion:
        assert projection['_inboxInjects'][0]['previews'][0]['text'] == completion['value']


def test_failed_advice_note_preserves_other_lane_receipts_and_retries(monkeypatch):
    import lib.agent_inbox as inbox
    import lib.tasks_pkg.segments as segments
    lanes = {
        'swarm': [{'value': 'completion', 'agent_id': 'child'}],
        'peer-msg': [{'value': 'peer', 'queueId': 'peer-row'}],
        'user-steer': [{'value': 'steer', 'queueId': 'steer-row'}],
        'background-command': [{'value': 'background', 'queueId': 'bg-row'}],
    }

    def drain(_key, **kwargs):
        lane = 'swarm' if 'exclude_modes' in kwargs else kwargs['modes'][0]
        return lanes.pop(lane, [])

    monkeypatch.setattr(inbox, 'drain', drain)
    advice = {'value': 'path overlap advice'}
    task = {'id': 'root', 'convId': 'conv',
            '_attemptId': 'attempt', '_projectOverlapAdvisories': [advice]}
    messages, events = [], []
    monkeypatch.setattr(_swarm_inbox, 'append_event', lambda task, event: events.append(event))
    record_note = segments.record_injected_note

    def failed_note(*args, **kwargs):
        raise RuntimeError('injected advice recorder failure')

    monkeypatch.setattr(segments, 'record_injected_note', failed_note)
    _swarm_inbox.drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='root')
    assert len(messages) == 1 and 'path overlap advice' not in messages[0]['content']
    assert not task.get(INJECTED_NOTES_FIELD)
    assert task['_projectOverlapAdvisories'] == [advice]
    assert len(events) == 1
    assert task['_inboxInjects']
    assert not task.get('_peerInjects') and not task.get('_userSteerInjects')
    for field in ('_swarm_inject_pending', '_peer_inject_pending',
                  '_steer_inject_pending', '_bgcmd_inject_pending'):
        assert task[field], field
    # No mailbox ACK: all delivered lane items still await provider confirmation.
    assert task['_peer_inject_pending'][0]['queueId'] == 'peer-row'

    monkeypatch.setattr(segments, 'record_injected_note', record_note)
    _swarm_inbox.drain_and_inject_inbox(task=task, messages=messages, round_num=2, tid='root')
    assert len(messages) == 2 and messages[1]['content'] == advice['value']
    assert not task.get('_projectOverlapAdvisories')
    assert len(task['_inboxInjects']) == 1
    source = capture_task_projection_source(task, {'type': 'llm_round_start'})
    projection = _task_projection(source, {})
    note, = [s for s in projection['segments'] if s.get('reason') == 'inbox_delivery']
    assert note['text'] == advice['value'] and note['llmRound'] == 2
    assert len(events) == 1  # advice creates no synthetic completion event


@pytest.mark.parametrize('failure_boundary', ['counter', 'manifest'])
def test_unappended_carrier_requeues_without_becoming_consumption_ready(monkeypatch, failure_boundary):
    import lib.agent_inbox as inbox
    from lib.tasks_pkg.context_composer import _render, injections

    swarm = {'agent_id': 'worker', 'value': 'completion', 'priority': 'urgent'}
    peer = {'queueId': 'peer-row', 'value': 'peer'}
    steer = {'queueId': 'steer-row', 'value': 'steer'}
    background = {'queueId': 'bg-row', 'value': 'background'}
    lanes = {'peer-msg': peer, 'user-steer': steer, 'background-command': background}
    advice = {'value': 'overlap'}
    task = {'id': 'task-id', 'convId': 'swarm-conv', '_peer_drain_key': 'peer-conv',
            '_projectOverlapAdvisories': [advice]}
    messages, requeued, events = [], [], []

    def drain(_key, **kwargs):
        return [swarm] if 'exclude_modes' in kwargs else [lanes[kwargs['modes'][0]]]

    def fail_count(*args, **kwargs):
        raise RuntimeError('injected preappend failure')

    monkeypatch.setattr(inbox, 'drain', drain)
    monkeypatch.setattr(inbox, 'enqueue', lambda key, value, **kw: requeued.append((key, value, kw)))
    if failure_boundary == 'counter':
        monkeypatch.setattr(_render, '_count_tokens', fail_count)
    else:
        monkeypatch.setattr(injections, 'record_manifest_rows', fail_count)
    monkeypatch.setattr(_swarm_inbox, 'append_event', lambda *args: events.append(args))
    _swarm_inbox.drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='log-only')
    assert not messages and not events
    for field in ('_swarm_inject_pending', '_peer_inject_pending', '_steer_inject_pending',
                  '_bgcmd_inject_pending', '_inboxInjects'):
        assert not task.get(field), field
    assert task['_projectOverlapAdvisories'] == [advice]
    assert [(key, kw['mode'], kw['extra']) for key, _, kw in requeued] == [
        ('swarm-conv', 'swarm-update', swarm), ('peer-conv', 'peer-msg', peer),
        ('swarm-conv', 'user-steer', steer), ('swarm-conv', 'background-command', background)]
    assert requeued[0][2]['priority'] == 'urgent'
