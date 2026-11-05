"""A worker's swarm card stops reading "waiting for model" once its model streams.

The child chat loop's ``waiting_model`` phase has no closing frame; the browser
ends a root model wait at its first delta, but a swarm card never receives a
worker's deltas. The SubAgent restates the card's word itself, once per change.
"""
import pytest

from lib.swarm.agent import SubAgent
from lib.swarm.types import SubTaskSpec

pytestmark = pytest.mark.unit


def _agent(events):
    return SubAgent(SubTaskSpec(role='general', objective='inspect'),
                    parent_task={'id': 'card-phase-parent', 'config': {}}, all_tools=[],
                    model='fixture', on_event=events.append)


def _card_words(events):
    return [(event['phase'], event['status']) for event in events
            if event.get('type') == 'swarm_agent_phase']


def test_first_streamed_text_ends_the_model_wait_once_per_change():
    events = []
    agent = _agent(events)
    agent._on_invocation_event({'type': 'phase', 'phase': 'waiting_model', 'detail': 'Waiting'})
    for _ in range(3):
        agent._on_invocation_event({'type': 'delta', 'thinking': 'weighing the options'})
    for _ in range(3):
        agent._on_invocation_event({'type': 'delta', 'content': 'The answer'})
    assert _card_words(events) == [
        ('waiting_model', 'running'), ('thinking', 'running'), ('writing', 'running')]


def test_each_new_phase_or_tool_call_rearms_the_streaming_word():
    events = []
    agent = _agent(events)
    agent._child = {'id': 'child'}
    agent._on_invocation_event({'type': 'delta', 'content': 'Reading first'})
    agent._on_invocation_event({'type': 'tool_start', 'toolCallId': 'call', 'toolName': 'read_files',
                                'roundNum': 0, 'query': 'a.py'})
    agent._on_invocation_event({'type': 'delta', 'content': 'Then answering'})
    agent._on_invocation_event({'type': 'phase', 'phase': 'waiting_model', 'detail': 'Waiting'})
    agent._on_invocation_event({'type': 'delta', 'content': 'Next round'})
    assert _card_words(events) == [('writing', 'running'), ('writing', 'running'),
                                   ('waiting_model', 'running'), ('writing', 'running')]
    assert [event['phase'] for event in events if event.get('type') == 'swarm_agent_tool_call'] == ['tool_use']
