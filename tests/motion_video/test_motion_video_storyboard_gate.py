"""tests/motion_video/test_motion_video_storyboard_gate.py — interactive storyboard gate (C4).

Executable specification for the motion storyboard gate: chat-initiated
jobs park after scenes.json lands; approval proceeds unchanged, a change
request feeds ONE bounded revision pass (gate failures keep the reviewed
board), and the decision persists so a crash-respawned job never re-asks.
The projection shares the slides outline gate's pendingGate block.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

_SCENES = [
    {'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。',
     'shot_recipe': 'hook-counter-burst'},
    {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '第二句。',
     'shot_recipe': 'messaging-multi-phrase'},
]
_SPAN = (0.0, 6.0)


def _task(tmp_path, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='',
                            workdir=str(tmp_path), voice='', speed=None,
                            alignment='loose', narration=True,
                            quality='draft', parallel=1, width=1080,
                            height=1440, user_id=TEST_OWNER_USER_ID)
    task['interactive_storyboard'] = True
    task['topic'] = '测试主题'
    task.update(over)
    return task


def _gate(task, scenes=None):
    from lib.motion_video.engine import _interactive_storyboard_gate
    return _interactive_storyboard_gate(
        task, task['workdir'], scenes or [dict(s) for s in _SCENES], _SPAN)


def test_headless_job_passes_through(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda *a, **k: called.append(1) or 'ok')
    task = _task(tmp_path, interactive_storyboard=False)
    out = _gate(task)
    assert out is not None and called == []
    assert not os.path.exists(
        os.path.join(str(tmp_path), 'storyboard_gate.json'))


def test_approval_proceeds_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '继续')[1])
    events = []
    task = _task(tmp_path)
    task['_emit_capture'] = events
    import lib.motion_video.engine as engine
    orig_emit = engine._emit
    monkeypatch.setattr(engine, '_emit',
                        lambda t, ev: events.append(ev) or orig_emit(t, ev))
    scenes = _gate(task)
    assert scenes is not None
    assert events[0]['type'] == 'storyboard_gate'
    assert events[0]['guidance_id'] == f"hg_{task['task_id']}_storyboard"
    assert events[0]['pages'][0]['key_message'] == '第一句。'
    assert events[1]['type'] == 'storyboard_gate_resolved'
    marker = json.loads((tmp_path / 'storyboard_gate.json').read_text(
        encoding='utf-8'))
    assert marker['resolved'] is True and marker['revised'] is False


def test_resolved_marker_skips_gate_on_crash_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '继续')[1])
    task = _task(tmp_path)
    _gate(task)
    called = []
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda *a, **k: called.append(1) or 'x')
    out = _gate(task)
    assert out is not None and called == []   # never re-asks


def test_feedback_drives_one_bounded_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '把第二镜改成讲成本而不是效率')[1])
    revised = json.dumps([
        {'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。'},
        {'id': 'scene-002', 'start': 3.0, 'end': 6.0,
         'text': '成本因此显著下降。'}], ensure_ascii=False)
    calls = []
    monkeypatch.setattr(
        'lib.llm_dispatch.api.dispatch_chat',
        lambda messages, **kw: calls.append(messages) or (revised, {}))
    task = _task(tmp_path)
    out = _gate(task)
    assert out is not None
    assert out[1]['text'] == '成本因此显著下降。'
    assert len(calls) == 1
    assert '成本' in calls[0][0]['content']
    marker = json.loads((tmp_path / 'storyboard_gate.json').read_text(
        encoding='utf-8'))
    assert marker['revised'] is True


def test_invalid_revision_keeps_the_reviewed_board(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '全部推翻重写')[1])
    # A revision whose spans no longer cover the narration timeline.
    bad = json.dumps([
        {'id': 'scene-001', 'start': 0.0, 'end': 2.0, 'text': '一。'},
        {'id': 'scene-002', 'start': 4.0, 'end': 5.0, 'text': '二。'}])
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat',
                        lambda messages, **kw: (bad, {}))
    task = _task(tmp_path)
    original = [dict(s) for s in _SCENES]
    out = _gate(task, scenes=original)
    assert [s['text'] for s in out] == ['第一句。', '第二句。']


def test_abort_at_the_gate_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), None)[1])
    task = _task(tmp_path)
    task['abort_event'].set()
    assert _gate(task) is None


def test_projection_pairs_storyboard_gate():
    from lib.production.work_unit import project_work_unit
    gate_event = {'type': 'storyboard_gate',
                  'guidance_id': 'hg_m1_storyboard', 'title': 'T',
                  'pages': [{'page': 1, 'pageType': 'hook',
                             'key_message': '开场'}]}
    task = {'id': 'motion_1', 'kind': 'motion-video', 'status': 'running',
            'meta': {'topic': 'T'}, 'events': [gate_event]}
    unit = project_work_unit(task)
    assert unit['pendingGate']['guidanceId'] == 'hg_m1_storyboard'
    task['events'].append({'type': 'storyboard_gate_resolved',
                           'guidance_id': 'hg_m1_storyboard'})
    assert 'pendingGate' not in project_work_unit(task)


def test_storyboard_real_wait_accepts_immediate_answer(tmp_path, monkeypatch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    def emit(task, event):
        if event['type'] == 'storyboard_gate':
            assert human_guidance.resolve_human_guidance(
                event['guidance_id'], 'ok', owner_user_id=TEST_OWNER_USER_ID)
    monkeypatch.setattr('lib.motion_video.engine._emit', emit)
    assert _gate(_task(tmp_path)) is not None
    assert len(registry) == 0


def test_storyboard_cancel_response_never_persists_approval(tmp_path, monkeypatch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    def emit(task, event):
        if event['type'] == 'storyboard_gate':
            assert human_guidance.cancel_human_guidance(
                event['guidance_id'], owner_user_id=TEST_OWNER_USER_ID)
    monkeypatch.setattr('lib.motion_video.engine._emit', emit)
    assert _gate(_task(tmp_path)) is None
    assert not (tmp_path / 'storyboard_gate.json').exists()
    assert len(registry) == 0


def test_storyboard_stop_propagates_into_real_wait(tmp_path, monkeypatch):
    import threading
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    stop = threading.Event()
    task = _task(tmp_path, abort_event=stop)
    monkeypatch.setattr('lib.motion_video.engine._emit',
                        lambda task, event: stop.set())
    assert _gate(task) is None
    assert len(registry) == 0
    assert not (tmp_path / 'storyboard_gate.json').exists()
