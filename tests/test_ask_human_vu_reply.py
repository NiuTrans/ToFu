"""tests/test_ask_human_vu_reply.py — ask_human autopilot branch must feed the
VU's TEXT, not the whole reply dict (pt_5355329b2838404f).

``run_virtual_user`` returns ``{'text', 'rounds', 'segments'}``.  The
ask_human autopilot branch used the whole dict as the human's answer, so
``f'Human response: {user_response}'`` stringified rounds/segments metadata
into the tool result fed back to the model — and the deliberately-kept
``[PROGRESS: ...]`` machine line in ``text`` leaked into model context via
this tool-result path (independent of the persistence-path leak fixed in
pt_0ae59e94 / 75ae1beb).

The fix: take ``vu_reply['text']`` and pass it through the single
``lib.agent_verdict.strip_machine_tokens`` predicate — the ask_human
tool-result path has NO consumer that needs the raw PROGRESS line (unlike
the budget guard).  Also covers the ``human_guidance_response`` SSE event,
which carried the same dict to the frontend.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

VU_TEXT = '继续做第三条，完成后跑一遍回归。\n[PROGRESS: resolved=2 remaining=3]'
VU_CLEAN = '继续做第三条，完成后跑一遍回归。'


@pytest.fixture
def vu_env(monkeypatch):
    """Drive the REAL _handle_ask_human with autopilot ON and heavy deps faked."""
    import lib.tasks_pkg.handlers.misc._human as human_handlers

    captured = {'events': [], 'resolved': []}

    monkeypatch.setattr('lib.tasks_pkg.autopilot.is_autopilot_enabled',
                        lambda t: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.autopilot.run_virtual_user',
        lambda task, vu_msg_id=None: {
            'text': VU_TEXT,
            'rounds': [{'toolName': 'read_files', 'status': 'done'}],
            'segments': [{'type': 'assistant', 'content': VU_TEXT}],
        })
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.resolve_human_guidance',
        lambda guidance_id, response: captured['resolved'].append(response))
    monkeypatch.setattr(human_handlers, 'append_event',
                        lambda task, ev: captured['events'].append(ev))
    monkeypatch.setattr(human_handlers, '_build_simple_meta', lambda *a, **k: {'k': k})
    monkeypatch.setattr(human_handlers, '_finalize_tool_round', lambda *a, **k: None)

    round_entry = {}
    fn_args = {'question': '下一步做什么？', 'response_type': 'free_text'}
    task = {'id': 'task-ask-0001', 'messages': []}
    tc_id, tool_content, _ = human_handlers._handle_ask_human(
        task, {}, 'ask_human', 'tc1', fn_args, 1, round_entry,
        {}, '', False, None)
    return captured, tool_content, round_entry


# ── 1. Behavioral: model gets the clean TEXT, never the dict repr ─────────

def test_tool_content_is_clean_vu_text_not_dict_repr(vu_env):
    _, tool_content, _ = vu_env
    # Exact shape: the answer prose only, machine line stripped.
    assert tool_content == f'Human response: {VU_CLEAN}'
    # The dict-repr giveaways must all be absent.
    assert '[PROGRESS' not in tool_content
    assert "'segments'" not in tool_content
    assert "'rounds'" not in tool_content
    assert "'text'" not in tool_content


def test_resolve_and_sse_event_carry_the_same_clean_text(vu_env):
    captured, _, _ = vu_env
    assert captured['resolved'] == [VU_CLEAN]
    hg_events = [e for e in captured['events']
                 if e.get('type') == 'human_guidance_response']
    assert len(hg_events) == 1
    assert hg_events[0]['response'] == VU_CLEAN
    assert hg_events[0]['isVirtualUser'] is True


# ── 2. Edge paths preserved ───────────────────────────────────────────────

def test_vu_none_still_means_aborted(monkeypatch):
    import lib.tasks_pkg.handlers.misc._human as human_handlers
    monkeypatch.setattr('lib.tasks_pkg.autopilot.is_autopilot_enabled',
                        lambda t: True)
    monkeypatch.setattr('lib.tasks_pkg.autopilot.run_virtual_user',
                        lambda task, vu_msg_id=None: None)
    monkeypatch.setattr(human_handlers, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, '_build_simple_meta', lambda *a, **k: {'k': k})
    monkeypatch.setattr(human_handlers, '_finalize_tool_round', lambda *a, **k: None)
    _, tool_content, _ = human_handlers._handle_ask_human(
        {'id': 'task-ask-0002', 'messages': []}, {}, 'ask_human', 'tc1',
        {'question': 'q', 'response_type': 'free_text'}, 1, {},
        {}, '', False, None)
    assert 'aborted' in tool_content


def test_empty_vu_text_falls_back_to_no_further_input(monkeypatch):
    import lib.tasks_pkg.handlers.misc._human as human_handlers
    monkeypatch.setattr('lib.tasks_pkg.autopilot.is_autopilot_enabled',
                        lambda t: True)
    # A reply that is ONLY a machine line strips down to empty.
    monkeypatch.setattr(
        'lib.tasks_pkg.autopilot.run_virtual_user',
        lambda task, vu_msg_id=None: {
            'text': '[PROGRESS: resolved=0 remaining=1]',
            'rounds': [], 'segments': []})
    monkeypatch.setattr('lib.tasks_pkg.human_guidance.resolve_human_guidance',
                        lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, '_build_simple_meta', lambda *a, **k: {'k': k})
    monkeypatch.setattr(human_handlers, '_finalize_tool_round', lambda *a, **k: None)
    _, tool_content, _ = human_handlers._handle_ask_human(
        {'id': 'task-ask-0003', 'messages': []}, {}, 'ask_human', 'tc1',
        {'question': 'q', 'response_type': 'free_text'}, 1, {},
        {}, '', False, None)
    assert tool_content == 'Human response: (no further input)'
