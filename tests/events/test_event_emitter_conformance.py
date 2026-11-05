"""Build-then-mutate emitters deliver only frames their EventSpec declares.

``build_event`` validates construction kwargs. The swarm worker
(``SwarmEvent.to_legacy`` metadata), the swarm driver's terminal
``swarm_phase`` frames and the done-event checkpoint merge stamp most fields
afterwards, so their final shape met the contract only at the task delivery
seam: a production warning, and never in unit tests that capture frames at the
emitter's own sink. Each test drives the REAL emitter and validates every frame
it hands to that sink.

If this fails, declare the field (``FieldSpec`` + prose in
``lib/agent_core/events.py``, then regenerate the derived contracts) when the
emitter is right, or fix the emitter when the declaration is.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from lib import agent_inbox
from lib.agent_core.events import validate_event
from lib.swarm.agent import SubAgent
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubAgentResult, SubTaskSpec
from lib.tasks_pkg.orchestrator._completion_receipt import build_done_event

pytestmark = pytest.mark.unit


def _violations(frames):
    return [(frame.get('type'), violation)
            for frame in frames for violation in validate_event(frame)]


def test_swarm_worker_progress_and_tool_call_frames_conform():
    frames = []
    agent = SubAgent(SubTaskSpec(id='worker', role='coder', objective='edit'),
                     parent_task={'id': 'wire-parent', 'config': {}}, all_tools=[],
                     model='fixture', on_event=frames.append)
    agent._child = {'id': 'child', 'model': 'fixture-fallback'}
    ref = {'taskId': 'child', 'roundNum': 0, 'turn': 'r1'}
    now = int(time.time() * 1000)
    agent._on_invocation_event({'type': 'round_start', 'roundNum': 0})
    agent._on_invocation_event({
        'type': 'tool_start', 'toolCallId': 'edit', 'toolName': 'write_file',
        'roundNum': 0, 'query': 'a.py ' + 'x' * 1000, 'inspectorRef': ref,
        'toolArgs': json.dumps({'path': 'a.py', 'content': 'x'}), 'tStart': now})
    agent._on_invocation_event({
        'type': 'tool_result', 'toolCallId': 'edit', 'roundNum': 0,
        'results': [{'path': 'a.py'}]})
    agent._on_invocation_event({
        'type': 'tool_complete', 'toolCallId': 'edit', 'roundNum': 0,
        'toolContent': 'written', 'tEnd': now + 5})
    # No inspector evidence and unparseable arguments: the optional
    # presentation fields ride the wire as null.
    agent._on_invocation_event({
        'type': 'tool_start', 'toolCallId': 'read', 'toolName': 'read_files',
        'roundNum': 0, 'query': 'b.py', 'toolArgs': 'not json', 'tStart': now})
    agent._on_invocation_event({
        'type': 'tool_complete', 'toolCallId': 'read', 'roundNum': 0,
        'toolContent': 'contents', 'tEnd': now + 7})
    agent._on_invocation_event({
        'type': 'tool_start', 'toolCallId': 'fail', 'toolName': 'read_files',
        'roundNum': 0, 'query': 'c.py', 'tStart': now})
    agent._on_invocation_event({
        'type': 'tool_complete', 'toolCallId': 'fail', 'roundNum': 0,
        'toolContent': 'boom', 'isError': True, 'tEnd': now + 9})

    progress = [frame for frame in frames if frame['type'] == 'swarm_agent_progress']
    calls = [frame for frame in frames if frame['type'] == 'swarm_agent_tool_call']
    assert progress and progress[0]['model'] == 'fixture-fallback'
    edit = [frame for frame in calls if frame['callId'] == 'child:edit:0']
    assert edit[0]['argsBriefTruncated'] is True
    assert edit[0]['argsBriefFullChars'] == len('a.py ' + 'x' * 1000)
    assert edit[-1]['inspectorRef'] == ref and edit[-1]['editedPath'] == 'a.py'
    assert {frame['callStatus'] for frame in calls} == {'running', 'done', 'failed'}
    assert _violations(frames) == []


@pytest.fixture
def master(monkeypatch):
    key = 'wire-' + uuid.uuid4().hex
    m = MasterOrchestrator(key, key, [SubTaskSpec(id='child', role='coder', objective='edit')],
                           user_id=1)
    monkeypatch.setattr(m, '_persist_agent_snapshot', lambda **kw: None)
    yield m
    agent_inbox.drain(key)


def test_swarm_driver_terminal_phase_frames_conform(master):
    class CrashingScheduler:
        running_count = 0
        pending_count = 0

        def started_at_map(self):
            return {}

        def iter_completions(self):
            master._on_agent_complete_callback(
                master.specs[0], SubAgentResult(status='completed', final_answer='done'))
            raise RuntimeError('driver fixture crash')

        def shutdown(self):
            pass

    frames = []
    master.on_progress = frames.append
    master._scheduler = CrashingScheduler()
    master._start_driver()
    master._driver_thread.join(3)

    assert master.is_terminated
    phases = [frame for frame in frames if frame['type'] == 'swarm_phase']
    assert [frame['phase'] for frame in phases] == ['error', 'complete']
    assert phases[-1]['outcome'] == 'failed'
    assert phases[-1]['reasonCodes'] == ['driver_error']
    assert phases[-1]['outcomeCounts']['completed'] == 1
    assert _violations(frames) == []


def test_done_event_carries_checkpoint_merged_file_count():
    files = [{'path': 'a.py', 'action': 'edited'}, {'path': 'b.py', 'action': 'written'}]
    task = {'id': 'wire-done', '_checkpointModifiedFileList': files,
            'modifiedFilesTruncated': True}

    done = build_done_event(
        task, cfg={}, api_rounds=[], last_finish_reason='stop', last_stream_result=None,
        accumulated_usage={}, last_usage=None, model='fixture', thinking_depth=None)

    assert done['modifiedFileList'] == files
    assert done['modifiedFiles'] == task['modifiedFiles'] == 2
    assert done['modifiedFilesTruncated'] is True
    assert _violations([done]) == []
