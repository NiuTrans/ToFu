"""tests/slides/test_slides_outline_gate.py — interactive outline-gate pins.

Executable specification for the outline_gate stage (A6): chat-initiated
decks park before the authoring fan-out; approval proceeds unchanged, a
change request feeds ONE bounded revision pass (validation failure keeps
the original plan), and headless jobs skip with zero behavioral footprint.
The gate's checkpoint (not the outline stage's) is what authoring reads
via ``_effective_outline`` — on live AND crash-resume paths.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import lib.slides.recipe as recipe
from lib.slides.recipe import _effective_outline, _run_outline_gate

_OUTLINE = {
    'title': '测试 deck', 'scenario': 'tech-engineering',
    'theme_id': 'paper-engineer',
    'pages': [
        {'pageType': 'cover', 'key_message': '开场', 'content_notes': ''},
        {'pageType': 'content', 'key_message': '正文', 'content_notes': ''},
        {'pageType': 'final', 'key_message': '收尾', 'content_notes': ''},
    ],
}


def _ctx(**over):
    ctx = {'topic': '测试主题', 'lang': 'zh', 'max_pages': 12,
           'model': None, 'owner_user_id': 1,
           'artifacts': {'outline': dict(_OUTLINE), 'research': {}},
           'abort_check': lambda: False, 'max_429_attempts': 1,
           'interactive_outline': True, 'task_id': 'slides_t1',
           'emit': None}
    ctx.update(over)
    return ctx


def test_headless_job_skips_with_zero_footprint():
    events = []
    ctx = _ctx(interactive_outline=False, emit=events.append)
    artifact = _run_outline_gate(ctx)
    assert artifact == {'skipped': True}
    assert events == []                      # no event, no wait


def test_approval_proceeds_unchanged(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '继续')[1])
    ctx = _ctx()
    artifact = _run_outline_gate(ctx)
    assert artifact['approved'] is True
    assert artifact['revised'] is False
    assert artifact['outline'] is ctx['artifacts']['outline']


def test_gate_event_carries_pages_then_resolved(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), 'ok')[1])
    events = []
    ctx = _ctx(emit=events.append)
    _run_outline_gate(ctx)
    assert events[0]['type'] == 'outline_gate'
    assert events[0]['guidance_id'] == 'hg_slides_t1_outline'
    assert [p['page'] for p in events[0]['pages']] == [1, 2, 3]
    assert events[1]['type'] == 'outline_gate_resolved'


def test_feedback_drives_one_bounded_revision(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '把标题改成「新标题」,删掉最后一页')[1])
    revised_json = ('{"title": "新标题", "scenario": "tech-engineering",'
                    ' "pages": ['
                    ' {"pageType": "cover", "key_message": "开场",'
                    '  "content_notes": ""},'
                    ' {"pageType": "content", "key_message": "正文",'
                    '  "content_notes": ""},'
                    ' {"pageType": "final", "key_message": "收尾",'
                    '  "content_notes": ""}]}')
    calls = []
    monkeypatch.setattr(
        recipe, '_llm_chat',
        lambda messages, **kw: calls.append(messages) or (revised_json, {}))
    ctx = _ctx()
    artifact = _run_outline_gate(ctx)
    assert artifact['revised'] is True
    assert artifact['outline']['title'] == '新标题'
    assert len(artifact['outline']['pages']) == 3
    assert artifact['outline']['human_revised'] is True
    assert len(calls) == 1                              # ONE bounded pass
    assert '把标题改成' in calls[0][0]['content']


def test_invalid_revision_keeps_the_original_plan(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), '重写一切')[1])
    monkeypatch.setattr(recipe, '_llm_chat',
                        lambda messages, **kw: ('这不是 JSON', {}))
    ctx = _ctx()
    artifact = _run_outline_gate(ctx)
    assert artifact['revised'] is False
    assert artifact['outline'] is ctx['artifacts']['outline']


def test_effective_outline_prefers_the_gate_checkpoint():
    ctx = _ctx()
    assert _effective_outline(ctx['artifacts']) is ctx['artifacts']['outline']
    revised = dict(_OUTLINE, title='改后')
    ctx['artifacts']['outline_gate'] = {'approved': True, 'outline': revised}
    assert _effective_outline(ctx['artifacts']) is revised  # resume parity


def test_abort_at_the_gate_raises_stage_aborted(monkeypatch):
    from lib.production.stages import StageAborted
    monkeypatch.setattr(
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        lambda guidance_id, **kw: (kw['on_registered'](), None)[1])
    ctx = _ctx(abort_check=lambda: True)
    with pytest.raises(StageAborted):
        _run_outline_gate(ctx)


def test_stage_registered_between_design_and_author():
    names = [stage.name for stage in recipe.slides_recipe_stages()]
    assert names == ['research', 'outline', 'design', 'outline_gate',
                     'author', 'assets', 'render', 'layout_qa', 'visual_qa',
                     'export']


def test_outline_gate_real_wait_accepts_immediate_answer(monkeypatch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    def emit(event):
        if event['type'] == 'outline_gate':
            assert human_guidance.resolve_human_guidance(
                event['guidance_id'], 'ok', owner_user_id=1)
    assert _run_outline_gate(_ctx(emit=emit))['approved']
    assert len(registry) == 0


def test_outline_gate_stop_reaches_shared_wait_and_never_approves(monkeypatch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    from lib.production.stages import StageAborted
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    stopped = []
    with pytest.raises(StageAborted):
        _run_outline_gate(_ctx(emit=lambda event: stopped.append(True),
                              abort_check=lambda: bool(stopped)))
    assert len(registry) == 0
