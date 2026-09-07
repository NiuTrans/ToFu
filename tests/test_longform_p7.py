"""tests/test_longform_p7.py — Third recipe validates the substrate (P7).

Owner ruling 2026-07-26: *"third recipe first, then extract"*. So this suite
is not only a feature test — it is the **measurement** that tells P6 what to
extract. The long-form report capability was written against the substrate
exactly as it stands (``lib.production.stages`` + the slice-2 discovery
registry), and these tests record what it could reuse and what it had to
duplicate.

Why a report is a fair test: it is a different SHAPE from video — a text
deliverable instead of a binary render, no TTS, no per-scene fan-out, and a
**data-dependent stage list** (one stage per outline section), which the
static video stage list never exercised.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading

import pytest

pytestmark = pytest.mark.unit

import lib.longform.recipe as rec
from lib.production import stages as st


_CARDS = [
    {'title': 'Fusion milestone', 'url': 'https://example.org/fusion',
     'snippet': 'Net energy gain was reproduced in three consecutive shots.'},
    {'title': 'Tokamak basics', 'url': 'https://sci.example.com/tokamak',
     'snippet': 'Magnetic confinement holds plasma away from the vessel wall.'},
]


def _patch_research(monkeypatch, results=None):
    monkeypatch.setattr(rec, '_web_search',
                        lambda q, user_question='': list(
                            _CARDS if results is None else results))


def _patch_llm(monkeypatch, sections=('背景', '现状', '展望')):
    """Outline call returns the section list; section calls return prose."""
    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': '核聚变研究进展',
                                'sections': list(sections)},
                               ensure_ascii=False), {'total_tokens': 100})
        return ('这是一节正文。' * 40, {'total_tokens': 200})
    monkeypatch.setattr(rec, '_llm_chat', fake)


# ══════════════════════════════════════════════════════════
#  The capability works end to end
# ══════════════════════════════════════════════════════════

def test_report_end_to_end(monkeypatch, tmp_path):
    _patch_research(monkeypatch)
    _patch_llm(monkeypatch)
    out = rec.build_report_from_topic('核聚变', str(tmp_path), lang='zh',
                                      depth='brief')
    assert out['sections'] == 3
    assert out['sources'] == 2
    md = open(out['path'], encoding='utf-8').read()
    assert md.startswith('# 核聚变研究进展')
    for heading in ('背景', '现状', '展望'):
        assert f'## {heading}' in md
    # Every source is cited in the report (grounding discipline carried over).
    for c in _CARDS:
        assert c['url'] in md


def test_research_gate_rejects_ungrounded_run(monkeypatch, tmp_path):
    """Same fact discipline as the video recipe: no sourced cards → refuse."""
    _patch_research(monkeypatch, results=[{'title': 'x', 'snippet': 'no url'}])
    _patch_llm(monkeypatch)
    with pytest.raises(st.StageFailed) as ei:
        rec.build_report_from_topic('x', str(tmp_path))
    assert ei.value.stage == 'research'


def test_short_section_is_rejected_by_its_gate(monkeypatch, tmp_path):
    _patch_research(monkeypatch)

    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': 'T', 'sections': ['A', 'B', 'C']}),
                    {'total_tokens': 10})
        return ('too short', {'total_tokens': 10})
    monkeypatch.setattr(rec, '_llm_chat', fake)
    with pytest.raises(st.StageFailed) as ei:
        rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert ei.value.stage.startswith('section-')


def test_outline_enforces_the_depth_budget_and_unique_headings(monkeypatch):
    headings = [f'Section {index}' for index in range(12)]
    monkeypatch.setattr(
        rec, '_llm_chat',
        lambda *a, **k: (json.dumps({'title': 'T', 'sections': headings}), {}))
    ctx = {
        'topic': 'x', 'lang': 'en', 'depth': 'brief',
        'artifacts': {'research': {
            'cards': [{'point': 'fact', 'url': 'https://example.test'}],
        }},
    }

    outline = rec._run_outline(ctx)
    assert outline['sections'] == headings[:3], \
        'a brief report must not pay for model-invented excess sections'
    assert rec._gate_outline(ctx, outline) == []
    assert rec._gate_outline(ctx, {'sections': headings[:2]})
    assert rec._gate_outline(ctx, {'sections': ['A', 'a', 'B']}) == [
        'outline contains duplicate section headings']


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_section_prompts_put_all_shared_evidence_before_the_heading(lang):
    """Every sibling exposes the large reusable prefix before it diverges."""
    ctx = {
        'lang': lang,
        'depth': 'deep',
        'artifacts': {
            'outline': {'title': 'Stable report title'},
            'research': {'cards': [
                {'point': f'Grounded fact {index} ' + ('evidence ' * 72),
                 'url': f'https://example.test/source-{index}'}
                for index in range(30)
            ]},
        },
    }
    headings = ('Unique Alpha', 'Unique Beta', 'Unique Gamma')
    prefix = rec._section_prompt_prefix(ctx)
    prompts = [rec._section_prompt(ctx, heading, prefix=prefix)
               for heading in headings]
    common_prefix = os.path.commonprefix(prompts)

    assert common_prefix.startswith(prefix)
    assert len(prefix) > 20_000
    for prompt, heading in zip(prompts, headings):
        assert prompt.startswith(prefix)
        assert prompt.index('Grounded fact 29') < prompt.index(heading)


# ══════════════════════════════════════════════════════════
#  THE MEASUREMENT — what the substrate did and didn't give us
# ══════════════════════════════════════════════════════════

def test_data_dependent_sections_do_not_repeat_upstream_stages(
        monkeypatch, tmp_path):
    """One upstream pass feeds a separately checkpointed sibling batch."""
    _patch_research(monkeypatch)
    calls = {'research': 0, 'outline': 0}
    real_research, real_outline = rec._run_research, rec._run_outline
    monkeypatch.setattr(rec, '_run_research',
                        lambda ctx: (calls.__setitem__('research', calls['research'] + 1),
                                     real_research(ctx))[1])
    monkeypatch.setattr(rec, '_run_outline',
                        lambda ctx: (calls.__setitem__('outline', calls['outline'] + 1),
                                     real_outline(ctx))[1])
    _patch_llm(monkeypatch)
    rec.build_report_from_topic('核聚变', str(tmp_path), depth='brief')
    assert calls == {'research': 1, 'outline': 1}, (
        'the data-dependent section batch repeated an upstream stage')


def test_section_calls_overlap_without_exceeding_the_resource_budget(
        monkeypatch, tmp_path):
    _patch_research(monkeypatch)
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '2')
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', '3')
    headings = ('A', 'B', 'C')
    first_pair = threading.Barrier(2)
    lock = threading.Lock()
    concurrency = {'active': 0, 'peak': 0}
    written = []

    def fake(messages, **kw):
        assert kw['max_retries'] == 2
        assert kw['max_429_attempts'] == 3
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': 'T', 'sections': headings}), {})
        heading = next(item for item in headings
                       if f'「{item}」' in prompt or f'"{item}"' in prompt)
        with lock:
            concurrency['active'] += 1
            concurrency['peak'] = max(
                concurrency['peak'], concurrency['active'])
            written.append(heading)
        try:
            if heading in ('A', 'B'):
                first_pair.wait(timeout=2)
            return ('正文内容。' * 40, {'total_tokens': 10})
        finally:
            with lock:
                concurrency['active'] -= 1

    monkeypatch.setattr(rec, '_llm_chat', fake)
    rec.build_report_from_topic('x', str(tmp_path), depth='brief')

    assert concurrency['peak'] == 2
    assert set(written) == set(headings)


def test_crash_midway_resumes_without_redoing_finished_sections(
        monkeypatch, tmp_path):
    """A killed process must resume at the first UNWRITTEN section."""
    _patch_research(monkeypatch)
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    written = []

    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': 'T', 'sections': ['A', 'B', 'C']}),
                    {'total_tokens': 10})
        for h in ('A', 'B', 'C'):
            if f'「{h}」' in prompt or f'"{h}"' in prompt:
                written.append(h)
                if h == 'C' and 'boom' not in str(kw):
                    pass
                break
        return ('正文内容。' * 40, {'total_tokens': 10})

    monkeypatch.setattr(rec, '_llm_chat', fake)
    # First pass: let section C fail so the job dies after A and B checkpoint.
    orig_make = rec._make_section_stage

    def make_failing(index, heading, **kwargs):
        stage = orig_make(index, heading, **kwargs)
        if heading != 'C':
            return stage
        return st.Stage(stage.name, lambda ctx: (_ for _ in ()).throw(
            RuntimeError('killed')), gate=stage.gate, retry=0,
            checkpoint_version=stage.checkpoint_version)

    monkeypatch.setattr(rec, '_make_section_stage', make_failing)
    with pytest.raises(st.StageFailed):
        rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == ['A', 'B']

    # Second pass with C healthy: A and B must NOT be rewritten.
    written.clear()
    monkeypatch.setattr(rec, '_make_section_stage', orig_make)
    out = rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == ['C'], f'resumed pass re-wrote {written} (should be only C)'
    assert out['sections'] == 3


def test_changed_outline_heading_invalidates_only_its_section(
        monkeypatch, tmp_path):
    """A refreshed outline must not reuse prose written for an old heading."""
    _patch_research(monkeypatch)
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    phase = {'headings': ['A', 'B', 'C']}
    written = []

    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': 'T', 'sections': phase['headings']}),
                    {'total_tokens': 10})
        for heading in phase['headings']:
            if f'「{heading}」' in prompt or f'"{heading}"' in prompt:
                written.append(heading)
                break
        return ('正文内容。' * 40, {'total_tokens': 10})

    monkeypatch.setattr(rec, '_llm_chat', fake)
    first = rec.build_report_from_topic(
        'x', str(tmp_path), depth='brief')
    assert written == ['A', 'B', 'C']

    state_path = tmp_path / 'pipeline_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['stages'].pop('outline')
    state_path.write_text(json.dumps(state), encoding='utf-8')

    phase['headings'] = ['A', 'B', 'D']
    written.clear()
    second = rec.build_report_from_topic(
        'x', str(tmp_path), depth='brief')
    assert written == ['D'], \
        'unchanged sections should resume while the changed heading is rewritten'
    markdown = open(second['path'], encoding='utf-8').read()
    assert '## D' in markdown and '## C' not in markdown
    assert first['path'] == second['path']


def test_section_checkpoints_bind_title_and_fact_card_inputs(
        monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    phase = {
        'title': 'T1',
        'cards': list(_CARDS),
    }
    monkeypatch.setattr(
        rec, '_web_search',
        lambda query, user_question='': list(phase['cards']))
    headings = ('A', 'B', 'C')
    written = []

    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({
                'title': phase['title'], 'sections': headings,
            }), {})
        written.append(next(
            item for item in headings
            if f'「{item}」' in prompt or f'"{item}"' in prompt))
        return ('正文内容。' * 40, {})

    monkeypatch.setattr(rec, '_llm_chat', fake)
    rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == list(headings)

    state_path = tmp_path / 'pipeline_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['stages'].pop('outline')
    state_path.write_text(json.dumps(state), encoding='utf-8')
    phase['title'] = 'T2'
    written.clear()
    rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == list(headings)

    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['stages'].pop('research')
    state_path.write_text(json.dumps(state), encoding='utf-8')
    phase['cards'] = [dict(_CARDS[0], snippet='A materially changed fact.'),
                      _CARDS[1]]
    written.clear()
    rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == list(headings)


def test_source_label_change_reassembles_without_rewriting_sections(
        monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    phase = {'card_title': 'Original source label'}
    cards = [dict(_CARDS[0])]
    headings = ('A', 'B', 'C')
    written = []

    def search(_query, user_question=''):
        return [dict(cards[0], title=phase['card_title'])]

    def fake(messages, **kw):
        prompt = messages[0]['content']
        if 'sections' in prompt or '大纲' in prompt:
            return (json.dumps({'title': 'T', 'sections': headings}), {})
        written.append(next(
            item for item in headings
            if f'「{item}」' in prompt or f'"{item}"' in prompt))
        return ('正文内容。' * 40, {})

    monkeypatch.setattr(rec, '_web_search', search)
    monkeypatch.setattr(rec, '_llm_chat', fake)
    first = rec.build_report_from_topic('x', str(tmp_path), depth='brief')
    assert written == list(headings)
    assert phase['card_title'] in open(
        first['path'], encoding='utf-8').read()

    state_path = tmp_path / 'pipeline_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['stages'].pop('research')
    state_path.write_text(json.dumps(state), encoding='utf-8')
    phase['card_title'] = 'Refreshed source label'
    written.clear()
    second = rec.build_report_from_topic('x', str(tmp_path), depth='brief')

    assert written == []
    assert phase['card_title'] in open(
        second['path'], encoding='utf-8').read()


def test_capability_needed_no_bespoke_poll_or_abort_route():
    """P7's headline finding: the report capability ships with ZERO bespoke
    lifecycle routes — the generic /api/v1/tasks/* endpoints serve it, because
    slice 2 made kind discovery real. (Podcast, written before that, had to
    hand-write poll_podcast_task.)"""
    import lib.longform.engine as eng
    src = open(eng.__file__, encoding='utf-8').read()
    for token in ('@api_v1', 'Blueprint', 'route('):
        assert token not in src, f'longform engine declares its own {token}'


def test_longform_is_discovered_by_the_generic_task_api():
    from routes.api_v1.tasks import _registries
    reg = _registries()
    assert 'longform-report' in reg, (
        'the third capability is invisible to /api/v1/tasks — discovery '
        'regressed')
    rt = reg['longform-report']
    for attr in ('_lock', '_tasks', 'get', 'poll', 'abort', 'kind'):
        assert hasattr(rt, attr)


def test_concurrent_identical_starts_spawn_one_report(monkeypatch, tmp_path):
    import lib.longform.engine as eng
    import lib.longform.runtime as runtime

    monkeypatch.setattr(eng, 'longform_root', lambda: str(tmp_path))
    monkeypatch.setattr(runtime, '_cleanup_stale_longform_tasks', lambda: None)
    spawned = []
    monkeypatch.setattr(
        runtime._longform_runtime, 'spawn',
        lambda task_id, function, task: spawned.append(task_id))
    barrier = threading.Barrier(8)
    topic = f'atomic start {tmp_path.name}'

    def start(_index):
        barrier.wait(timeout=3)
        return eng.start_report_job(topic, lang='en', depth='brief', user_id=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(start, range(8)))

    task_ids = {result['task_id'] for result in results}
    task_id = next(iter(task_ids)) if task_ids else ''
    try:
        assert len(task_ids) == 1
        assert sum(not result['deduped'] for result in results) == 1
        assert spawned == [task_id]
    finally:
        if task_id:
            runtime._longform_runtime.discard(task_id)
            runtime._production.index_get((1, topic, 'en', 'brief'))


def test_conversation_artifact_publish_failure_cannot_finish_done(
        monkeypatch, tmp_path):
    import lib.artifacts.core as artifacts
    import lib.longform.engine as eng
    import lib.longform.runtime as runtime

    report_path = tmp_path / 'report.md'
    report_path.write_text('# Report\n\n' + ('complete body ' * 40),
                           encoding='utf-8')
    task_id = runtime._longform_task_id()
    task = runtime._longform_runtime.create(user_id=1, task_id=task_id)
    task.update({
        'task_id': task_id, 'user_id': 1, 'topic': 'topic',
        'workdir': str(tmp_path), 'lang': 'en', 'depth': 'brief',
        'conv_id': 'conv-required-publication',
    })
    manifest_states = []
    monkeypatch.setattr(
        eng, '_write_manifest',
        lambda task, state: manifest_states.append(state))
    monkeypatch.setattr(eng, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(
        'lib.longform.recipe.build_report_from_topic',
        lambda *a, **k: {
            'path': str(report_path), 'chars': report_path.stat().st_size,
            'sections': 3, 'sections_written': 3, 'sections_requested': 3,
            'sources': 2, 'title': 'Report',
        })
    monkeypatch.setattr(artifacts, 'create_artifact', lambda **kwargs: {})

    try:
        eng.run_longform_task(task)
        assert task['status'] == 'error'
        assert task['result'] is None
        assert 'did not confirm publication' in task['error']['detail']
        assert manifest_states == ['running', 'error']
    finally:
        runtime._longform_runtime.discard(task_id)


def test_recipe_is_the_only_place_that_knows_about_reports():
    """If the substrate is the right shape, report-specific knowledge lives in
    the recipe — the substrate must stay capability-agnostic."""
    import ast

    import lib.production.stages as sub
    src = open(sub.__file__, encoding='utf-8').read()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        for token in ('longform', 'motion_video', 'paper', 'tts'):
            assert token not in mod, (
                f'substrate imports {mod!r} — it is no longer capability-'
                f'agnostic, so the next recipe inherits this baggage')


def test_produce_report_tool_is_registered_and_ungated_by_project():
    from lib.tools.registry import ToolContext, assemble_tool_list
    ctx = ToolContext(cfg={}, task_id='t', project_path='',
                      project_enabled=False, search_mode='multi',
                      search_enabled=True, fetch_enabled=False,
                      code_exec_enabled=False, browser_enabled=False,
                      desktop_enabled=False)
    assemble_tool_list(ctx)
    names = {t['function']['name'] for t in ctx.executable_tool_catalog}
    assert 'produce_report' in names
    assert 'produce_video' in names
