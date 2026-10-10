"""Completion delivery keeps report XML intact in model and display projections."""
import pytest

from lib.agent_inbox import format_swarm_update
from lib.tasks_pkg.orchestrator import _swarm_inbox

pytestmark = pytest.mark.unit


def test_completion_report_survives_event_and_reload_projection(monkeypatch):
    report = 'REPORT HEAD\n' + ('中文报告 <evidence> & verified\n' * 300) + 'REPORT TAIL'
    payload = format_swarm_update(
        agent_id='reviewer', role='general', status='completed',
        elapsed_seconds=2285.2, tokens=2051923, result=report,
        output_file='/synthetic/reviewer.log')
    item = {'agent_id': 'reviewer', 'value': payload}
    task = {'id': 'synthetic-task', 'convId': 'synthetic-conversation'}
    messages = []
    events = []

    def drain(_key, **kwargs):
        return [item] if 'exclude_modes' in kwargs else []

    monkeypatch.setattr('lib.agent_inbox.drain', drain)
    monkeypatch.setattr(_swarm_inbox, 'append_event', lambda task, event: events.append(event))
    _swarm_inbox.drain_and_inject_inbox(
        task=task, messages=messages, round_num=2, tid='synthetic')

    assert len(messages) == 1
    assert payload in messages[0]['content']
    assert task['_swarm_inject_pending'] == [item]
    previews = task['_inboxInjects'][0]['previews']
    assert previews == [{'agentId': 'reviewer', 'text': payload}]
    injection = next(event for event in events if event.get('type') == 'swarm_inbox_inject')
    assert injection['previews'] == previews
    assert 'REPORT TAIL</result>' in previews[0]['text']
    # Report preservation must not manufacture provider-consumption acknowledgement.
    assert not task.get('_swarm_delivered_result_ids')
