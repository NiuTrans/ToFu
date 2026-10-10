"""Failures are execution evidence immediately, not only after batch settlement."""
from types import SimpleNamespace
import uuid

import pytest

from lib import agent_inbox
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubAgentResult, SubTaskSpec

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_terminal_child_evidence_reaches_ui_and_inbox_before_siblings_finish(monkeypatch, status):
    key = 'failure-delivery-' + uuid.uuid4().hex
    frames = []
    specs = [SubTaskSpec(id=name, role='coder', objective='bounded test')
             for name in ('failed-child', 'still-working')]
    master = MasterOrchestrator(key, key, specs, user_id=7, on_progress=frames.append)
    master._scheduler = SimpleNamespace(started_at_map=lambda: {'still-working': 1.0})
    monkeypatch.setattr(master, '_persist_agent_snapshot', lambda **kw: None)
    result = SubAgentResult(status=status, final_answer='partial inspection',
                            error_message='HTTP 403: model unavailable' if status == 'failed' else '',
                            tool_log=[{'tool': 'write_file'}])
    try:
        master._on_agent_complete_callback(specs[0], result)
        assert not master.is_terminated
        assert len(frames) == 1 and frames[0]['type'] == 'swarm_agent_complete'
        assert frames[0]['status'] == status
        assert frames[0]['error'] == result.error_message
        assert frames[0]['summary'] == result.final_answer
        updates = agent_inbox.pending_items(key, modes=['swarm-update'])
        assert len(updates) == 1 and updates[0]['status'] == status
        assert 'running="1"' in updates[0]['value']
        assert f'<status>{status}</status>' in updates[0]['value']
        master._on_agent_complete_callback(specs[0], result)
        assert len(frames) == 1
        assert len(agent_inbox.pending_items(key, modes=['swarm-update'])) == 1
    finally:
        agent_inbox.reset_for_test(key)


def test_error_cannot_become_success_because_partial_prose_exists(monkeypatch):
    key = 'failure-partial-' + uuid.uuid4().hex
    frames = []
    spec = SubTaskSpec(id='partial', role='coder', objective='bounded test')
    master = MasterOrchestrator(key, key, [spec], user_id=7, on_progress=frames.append)
    monkeypatch.setattr(master, '_persist_agent_snapshot', lambda **kw: None)
    try:
        master._on_agent_complete_callback(spec, SubAgentResult(
            status='completed', final_answer='Already edited some files.',
            error_message='Provider rejected the next request.'))
        assert frames[0]['status'] == 'failed'
        assert frames[0]['summary'] == 'Already edited some files.'
        assert agent_inbox.pending_items(key)[0]['status'] == 'failed'
    finally:
        agent_inbox.reset_for_test(key)
