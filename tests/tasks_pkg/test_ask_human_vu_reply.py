"""Human guidance cannot be answered by the retired standalone VU interpreter."""
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('autopilot_config', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_unattended_guidance_never_fabricates_human_input(monkeypatch, autopilot_config, batch):
    import lib.tasks_pkg.handlers.misc._human as human
    events = []
    monkeypatch.setattr(human, 'append_event', lambda task, ev: events.append(ev))
    monkeypatch.setattr(human, '_build_simple_meta', lambda *args, **kwargs: {})
    monkeypatch.setattr(human, '_finalize_tool_round', lambda *args, **kwargs: None)
    row = {}
    args = ({'questions': [{'question': 'May I proceed?', 'response_type': 'free_text'}, {'question': 'Which scope?', 'response_type': 'free_text'}]}
            if batch else {'question': 'May I proceed?', 'response_type': 'free_text'})
    _, content, _ = human._handle_ask_human(
        {'id': 'unattended', 'messages': [], 'config': {'autopilot': autopilot_config}},
        {}, 'ask_human', 'call', args, 1, row, {}, '', False, None)
    assert 'no interactive session' in content
    assert row['status'] == 'unanswerable'
    assert not any(event.get('isVirtualUser') for event in events)


def test_abort_leaves_round_awaiting_and_unfinalized(monkeypatch):
    """Abort must NOT finalize the round into a completed receipt.

    Regression pin for the card-collapse bug: the user pressed Stop while a
    choice question was open; the handler used to finalize the round with a
    placeholder result, the settlement then stopped offering
    answer_guidance, and the interactive card degraded into a dead static
    receipt. The fix leaves the round awaiting_human so the settlement's
    missing_tool_result handling offers the late-answer resume.
    """
    import lib.tasks_pkg.handlers.misc._human as human_handlers
    finalized = []
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, task=None, on_registered=None: on_registered())
    monkeypatch.setattr(human_handlers, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, '_build_simple_meta',
                        lambda *a, **k: {'k': k})
    monkeypatch.setattr(human_handlers, '_finalize_tool_round',
                        lambda *a, **k: finalized.append(k))
    round_entry = {}
    _, tool_content, _ = human_handlers._handle_ask_human(
        {'id': 'task-ask-0004', 'messages': [], '_attended': True,
         'aborted': True},
        {}, 'ask_human', 'tc1',
        {'question': 'q', 'response_type': 'choice',
         'options': [{'label': 'A'}, {'label': 'B'}]},
        1, round_entry, {}, '', False, None)
    assert tool_content == ('[Task was aborted while waiting for human '
                            'guidance]')
    assert finalized == []
    assert round_entry['status'] == 'awaiting_human'
    assert 'results' not in round_entry
    assert round_entry['guidanceType'] == 'choice'
    assert round_entry['guidanceOptions'] == [{'label': 'A'}, {'label': 'B'}]
