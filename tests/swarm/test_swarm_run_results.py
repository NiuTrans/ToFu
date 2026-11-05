"""Swarm results are durable user state keyed by ``swarm_run_id``.

A swarm session is keyed by the reusable conversation key. Its resumable rows
were deleted when the session expired after inactivity (TTL) and when a new
wave replaced a terminated session, taking every agent's final result with
them, and ``get_agent_result`` could not name an earlier wave at all. These
specs pin the contract through the production tool boundary and a real SQL
storage authority (the run records are portable generic records, so the
native authority stores them through the same operations):

* every settled outcome is recorded under (owner, swarm_run_id, agent_id)
  before the model is told, and ``<swarm-update>`` names its run;
* TTL eviction and new-wave replacement reclaim only resumable rows, never an
  unrecorded result (rows are kept when recording fails);
* ``get_agent_result(swarm_run_id=...)`` reads ended, evicted and earlier
  waves of the same conversation, scoped to its owner and conversation.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import uuid

import pytest

from lib import agent_inbox
from lib.storage.errors import StorageError
from lib.swarm import persistence
from lib.swarm.integration import _state
import lib.swarm.integration as integration
from lib.swarm.protocol import SubAgentResult, SubAgentStatus
from tests.swarm.test_swarm_async import _wait_until, execute_swarm_tool

pytestmark = pytest.mark.unit

OWNER = 7


class _CheckpointingAgent:
    """A finished worker that persists its terminal checkpoint like SubAgent."""

    def __init__(self, spec, answer, swarm_key):
        self.spec, self.swarm_key = spec, swarm_key
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.result = SubAgentResult(
            status=SubAgentStatus.COMPLETED.value, final_answer=answer,
            elapsed_seconds=0.05, total_tokens=42, rounds_used=1)

    def run(self):
        persistence.save_agent(
            self.swarm_key, self.spec.id, role=self.spec.role,
            objective=self.spec.objective, status=self.result.status,
            messages=[], result=self.result.to_dict(), rounds_used=1)
        return self.result


@pytest.fixture(scope='module')
def authority():
    from lib.storage import StorageSupervisor
    root = tempfile.TemporaryDirectory(prefix='tofu-swarm-run-results-')
    supervisor = StorageSupervisor(project_root=Path(root.name), backend='sqlite',
                                   startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()
        root.cleanup()


@pytest.fixture
def conversation(authority, monkeypatch):
    monkeypatch.setattr(persistence, '_storage', lambda **_kwargs: authority.client)
    # Wake policy has its own specs; these results are read on demand.
    monkeypatch.setattr(integration, 'SWARM_AUTOCONTINUE_ENABLED', False)
    conv = 'run-results-' + uuid.uuid4().hex[:12]
    agent_inbox.reset_for_test(conv)
    yield conv
    with _state._sessions_lock:
        _state._active_sessions.pop(conv, None)
        _state._session_timestamps.pop(conv, None)
    persistence.delete_session(conv)
    agent_inbox.reset_for_test(conv)
    assert _state.stop_swarm_cleanup_timer(timeout=10.0)


def _task(conv, turn_id, owner=OWNER):
    return {'id': turn_id, 'convId': conv, '_userId': owner, 'status': 'running'}


def _launch(conv, turn_id, answers, monkeypatch):
    def build(spec, **kwargs):
        return _CheckpointingAgent(spec, answers[spec.id], kwargs['swarm_key'])

    monkeypatch.setattr('lib.swarm.master._build_sub_agent', build)
    agents = [{'id': agent_id, 'role': 'researcher',
               'objective': f'Investigate {agent_id} for {turn_id}'}
              for agent_id in answers]
    handle = json.loads(execute_swarm_tool(
        'spawn_agents', {'agents': agents}, task=_task(conv, turn_id)))
    assert handle['status'] == 'async_launched', handle
    assert _wait_until(lambda: (
        integration.get_active_session(conv) is not None
        and integration.get_active_session(conv).task_id == turn_id
        and integration.get_active_session(conv).is_terminated), timeout=10.0)
    return handle


def _result(conv, turn_id, owner=OWNER, **args):
    return json.loads(execute_swarm_tool(
        'get_agent_result', args, task=_task(conv, turn_id, owner)))


def _expire(conv, monkeypatch):
    """Age the session past its TTL and run the production sweep to the end."""
    with _state._sessions_lock:
        _state._session_timestamps[conv] = time.time() - 10 * _state.SESSION_TTL_SECONDS
    monkeypatch.setattr(integration, '_last_cleanup', 0.0)
    monkeypatch.setattr(_state, '_last_cleanup', 0.0)
    integration.get_active_session('unrelated-' + conv)
    assert _state.stop_swarm_cleanup_timer(timeout=10.0)  # joins TTL teardown
    assert conv not in integration._active_sessions


def test_ttl_eviction_keeps_results_readable_by_swarm_run_id(conversation, monkeypatch):
    conv = conversation
    handle = _launch(conv, 'turn-1', {'a1': 'ANSWER-ONE'}, monkeypatch)
    assert handle['swarm_run_id'] == 'turn-1'
    updates = agent_inbox.pending_items(conv, modes=['swarm-update'])
    assert any('<swarm-run-id>turn-1</swarm-run-id>' in item['value'] for item in updates)

    _expire(conv, monkeypatch)
    assert persistence.load_session(conv) is None  # resumable rows reclaimed

    out = _result(conv, 'turn-2', agent_id='a1', swarm_run_id='turn-1')
    assert out['status'] == 'ok' and out['found'] is True
    assert (out['source'], out['swarm_run_id']) == ('run_record', 'turn-1')
    assert out['agent_status'] == 'completed'
    assert out['final_answer'] == 'ANSWER-ONE'
    assert out['role'] == 'researcher'
    assert out['objective'] == 'Investigate a1 for turn-1'
    # The evicted run is never guessed from a bare agent id; the miss names
    # the handle that reads it.
    miss = _result(conv, 'turn-2', agent_id='a1')
    assert miss['status'] == 'error' and miss['code'] == 'swarm_not_found'
    assert 'swarm_run_id' in miss['next_action']


def test_new_wave_replacement_keeps_the_earlier_wave_readable(conversation, monkeypatch):
    conv = conversation
    _launch(conv, 'turn-1', {'a1': 'WAVE-ONE'}, monkeypatch)
    second = _launch(conv, 'turn-2', {'a1': 'WAVE-TWO'}, monkeypatch)
    assert second['swarm_run_id'] == 'turn-2' and second['is_followup'] is False
    row = persistence.load_session(conv)
    assert row['task_id'] == 'turn-2'
    assert [(agent['agent_id'], agent['result']['final_answer'])
            for agent in row['agents']] == [('a1', 'WAVE-TWO')]

    current = _result(conv, 'turn-3', agent_id='a1')
    assert (current['final_answer'], current['swarm_run_id']) == ('WAVE-TWO', 'turn-2')
    earlier = _result(conv, 'turn-3', agent_id='a1', swarm_run_id='turn-1')
    assert (earlier['final_answer'], earlier['source']) == ('WAVE-ONE', 'run_record')
    named_current = _result(conv, 'turn-3', agent_id='a1', swarm_run_id='turn-2')
    assert named_current['final_answer'] == 'WAVE-TWO'
    batch = _result(conv, 'turn-3', agent_ids=['a1', 'never-ran'], swarm_run_id='turn-1')
    assert batch['results'][0]['final_answer'] == 'WAVE-ONE'
    assert batch['results'][1]['code'] == 'swarm_not_found'


def test_run_results_are_owner_and_conversation_scoped(conversation, monkeypatch):
    conv = conversation
    _launch(conv, 'turn-1', {'a1': 'PRIVATE-ANSWER'}, monkeypatch)
    _expire(conv, monkeypatch)

    foreign_owner = _result(conv, 'turn-2', owner=OWNER + 1,
                            agent_id='a1', swarm_run_id='turn-1')
    assert foreign_owner['status'] == 'error' and foreign_owner['found'] is False
    assert 'PRIVATE-ANSWER' not in json.dumps(foreign_owner)
    other_conversation = json.loads(execute_swarm_tool(
        'get_agent_result', {'agent_id': 'a1', 'swarm_run_id': 'turn-1'},
        task=_task(conv + '-other', 'turn-2')))
    assert other_conversation['status'] == 'error'
    assert 'PRIVATE-ANSWER' not in json.dumps(other_conversation)
    assert persistence.load_run_result(
        owner_id=OWNER + 1, swarm_run_id='turn-1', agent_id='a1') is None
    record = persistence.load_run_result(
        owner_id=OWNER, swarm_run_id='turn-1', agent_id='a1')
    assert (record['ownerId'], record['conversationId']) == (OWNER, conv)


def test_unrecorded_results_keep_the_resumable_rows(conversation, monkeypatch):
    conv = conversation
    real_command = persistence._command

    def records_unavailable(operation, payload, **kwargs):
        if operation == 'record.put':
            raise StorageError('database_unavailable', 'record store down')
        return real_command(operation, payload, **kwargs)

    monkeypatch.setattr(persistence, '_command', records_unavailable)
    _launch(conv, 'turn-1', {'a1': 'KEPT-ANSWER'}, monkeypatch)
    _expire(conv, monkeypatch)

    row = persistence.load_session(conv)
    assert row is not None and row['task_id'] == 'turn-1'  # never silently deleted
    kept = _result(conv, 'turn-2', agent_id='a1', swarm_run_id='turn-1')
    assert (kept['final_answer'], kept['source']) == ('KEPT-ANSWER', 'persisted')


def test_rows_without_an_orchestrator_are_archived_before_deletion(conversation):
    conv = conversation
    persistence.save_session(conv, conv_id=conv, task_id='turn-9', specs=[],
                             config={'user_id': OWNER, 'parent_cfg': {}})
    persistence.save_agent(conv, 'a9', role='coder', objective='Archive me',
                           status='failed', messages=[],
                           result={'status': 'failed', 'error_message': 'boom',
                                   'final_answer': 'partial evidence'})
    assert _state._record_results_then_delete(conv, None) is True
    assert persistence.load_session(conv) is None
    out = _result(conv, 'turn-10', agent_id='a9', swarm_run_id='turn-9')
    assert out['agent_status'] == 'failed' and out['final_answer'] == ''
    assert out['partial_output'] == 'partial evidence' and out['error'] == 'boom'

    # An owner that cannot be proven is never deleted on a guess.
    persistence.save_session(conv, conv_id=conv, task_id='turn-11', specs=[], config={})
    assert _state._record_results_then_delete(conv, None) is False
    assert persistence.load_session(conv)['task_id'] == 'turn-11'


def test_rows_left_by_an_earlier_run_are_recorded_before_a_new_run_claims_the_key(
        conversation, monkeypatch):
    conv = conversation
    # A terminated run that was not rehydrated after a restart: rows only.
    persistence.save_session(conv, conv_id=conv, task_id='turn-old', specs=[],
                             config={'user_id': OWNER, 'parent_cfg': {}},
                             status='terminated')
    persistence.save_agent(conv, 'old-agent', role='researcher', objective='Old work',
                           status='completed', messages=[],
                           result={'status': 'completed', 'final_answer': 'OLD-ANSWER'})
    _launch(conv, 'turn-new', {'new-agent': 'NEW-ANSWER'}, monkeypatch)

    row = persistence.load_session(conv)
    assert row['task_id'] == 'turn-new'
    assert [agent['agent_id'] for agent in row['agents']] == ['new-agent']
    old = _result(conv, 'turn-next', agent_id='old-agent', swarm_run_id='turn-old')
    assert (old['final_answer'], old['source']) == ('OLD-ANSWER', 'run_record')
    misattributed = _result(conv, 'turn-next', agent_id='old-agent',
                            swarm_run_id='turn-new')
    assert misattributed['status'] == 'error'


def test_invalid_swarm_run_id_is_rejected_before_any_read(conversation):
    out = _result(conversation, 'turn-1', agent_id='a1', swarm_run_id='  ')
    assert out['status'] == 'error' and out['code'] == 'invalid_swarm_run_id'


def test_tool_contract_names_the_run_handle():
    from lib.swarm.tools import GET_AGENT_RESULT_TOOL
    function = GET_AGENT_RESULT_TOOL['function']
    assert 'swarm_run_id' in function['parameters']['properties']
    assert 'evicted' in function['description'] and 'later wave' in function['description']
