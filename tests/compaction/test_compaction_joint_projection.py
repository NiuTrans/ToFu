"""L2 shares its cache rewrite with L1 without committing a failed candidate."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._layer2 import _compact as compact
from lib.tasks_pkg.compaction._layer2._anchor import _plan_cross_turn_tail
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
from tests.tasks_pkg.test_project_guidance_delta import (
    ROOT_BODY, SUB_BODY, OTHER_BODY, _project, _task, _round, _tool_round, _visible,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def boundary(monkeypatch):
    summaries, archives, events = [], [], []

    def summarize(messages, *args, **kwargs):
        summaries.append(deepcopy(messages))
        return '### Objective\nFinish the requested investigation.\n### Next Steps\nVerify.'

    def archive(conv_id, messages, **kwargs):
        archives.append(deepcopy(messages))
        return None

    monkeypatch.setattr(compact, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(compact, '_archive_transcript', archive)
    monkeypatch.setattr(compact, '_announce_summary_start', lambda *a: None)
    monkeypatch.setattr('lib.goal_runs.run_pins.update_objective_from_receipt', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: events.append(k))
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
                        lambda *a, **k: pytest.fail('L2 preconditioning reads a cache fence'))
    return summaries, archives, events


def image_history():
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Inspect the visual regression.'}]
    for index in range(18):
        messages.extend(_tool_round('inspect_image', {}, f'image-{index}'))
        messages[-1]['content'] = [
            {'type': 'text', 'text': f'Observed frame {index}'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A' * 12000}},
        ]
    return messages


def test_joint_candidate_cleans_retained_images_and_keeps_original_archive(boundary):
    summaries, archives, events = boundary
    messages = image_history()
    original = deepcopy(messages)
    task = {'id': 'joint', '_userId': 7, 'convId': 'joint-conv', 'config': {}}
    meta = {}
    compact.execute_compact_tool(messages, task, preserve_budget_tokens=4000, _result_meta=meta)
    assert meta['compacted']
    assert archives == [original]
    assert messages[-1].get('_compactionHandoff')
    assert original[-2] in messages and original[-1] in messages
    assert any(m.get('role') == 'tool' and isinstance(m.get('content'), str)
               and 'compacted' in m['content'] for m in messages)
    _, _, baseline_cold = _plan_cross_turn_tail(original, budget_tokens=4000, max_turns=16)
    assert len(summaries[0]) < len(baseline_cold)
    # Summary evidence still comes from the original observations, including
    # their media/recovery metadata; it never summarizes a fabricated result.
    assert all(m in original for m in summaries[0])
    assert not events  # One L2 adoption; no speculative L1 events.


@pytest.mark.parametrize('config', [{'disableDefaultL1': True}, {'steps': []}])
def test_l1_ablation_keeps_selected_tool_sources_verbatim(boundary, config):
    messages = image_history()
    original = deepcopy(messages)
    task = {'id': 'ablation', '_userId': 7, 'config': {'compaction': config}}
    compact.execute_compact_tool(messages, task, preserve_budget_tokens=4000)
    assert all(m in original for m in messages if m.get('role') == 'tool')


def test_invalid_structural_l1_step_cannot_silently_drop_source(boundary, monkeypatch):
    summaries, archives, events = boundary
    messages = image_history()
    original = deepcopy(messages)
    monkeypatch.setattr('lib.tasks_pkg.compaction._steps.run_steps',
                        lambda steps, ctx: ctx.messages.pop())
    with pytest.raises(ValueError, match='content-only'):
        compact.execute_compact_tool(messages, {'_userId': 7, 'config': {}})
    assert messages == original
    assert not summaries and not archives and not events


@pytest.mark.parametrize('failure', ['empty', 'exception', 'cancel', 'over_budget'])
def test_joint_failure_keeps_source_and_has_no_l1_side_effects(boundary, monkeypatch, failure):
    _, archives, events = boundary
    messages = image_history()
    original = deepcopy(messages)
    task = {'id': 'joint', '_userId': 7, 'config': {}}

    def summarize(*args, **kwargs):
        if failure == 'exception':
            raise RuntimeError('summary transport failed')
        if failure == 'cancel':
            task['aborted'] = True
        return '' if failure == 'empty' else 'complete'

    monkeypatch.setattr(compact, '_generate_query_aware_summary', summarize)
    options = {'preserve_budget_tokens': 4000}
    if failure == 'over_budget':
        options.update(_dispatch_prompt_target=1, _dispatch_prompt_measure=lambda m: 100)
    if failure == 'exception':
        with pytest.raises(RuntimeError, match='summary transport'):
            compact.execute_compact_tool(messages, task, **options)
    else:
        meta = {}
        compact.execute_compact_tool(messages, task, _result_meta=meta, **options)
        assert not meta['compacted']
    assert messages == original
    assert not archives and not events


@pytest.mark.parametrize('failure', [False, True])
def test_l2_retires_only_unretained_scopes_and_refresh_does_not_restore_them(
        tmp_path, boundary, monkeypatch, failure):
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'Investigate both components.'}]
    _round(task, messages, root / 'sub' / 'file.py', root / 'other' / 'file.py')
    messages.extend(_tool_round('read_files', {'path': str(root / 'other' / 'file.py')}, 'old'))
    messages[-1]['content'] = 'older source ' * 10000
    messages.extend(_tool_round('read_files', {'path': str(root / 'sub' / 'file.py')}, 'current'))
    original = deepcopy(messages)
    if failure:
        monkeypatch.setattr(compact, '_generate_query_aware_summary', lambda *a, **k: '')
    meta = {}
    compact.execute_compact_tool(messages, task, preserve_budget_tokens=1, _result_meta=meta)
    if failure:
        assert not meta['compacted'] and messages == original
        assert str(root / 'other' / 'AGENTS.md') in task['_projectGuidance']['rules']
        return
    assert meta['compacted']
    visible = _visible(messages)
    assert ROOT_BODY.strip() in visible and SUB_BODY.strip() in visible
    assert OTHER_BODY.strip() not in visible
    assert _estimate_total_tokens(messages) < _estimate_total_tokens(original)
    assert _round(task, messages) == ''
    assert str(root / 'other' / 'AGENTS.md') not in task['_projectGuidance']['rules']
    _round(task, messages, root / 'other' / 'file.py')
    assert OTHER_BODY.strip() in _visible(messages)
