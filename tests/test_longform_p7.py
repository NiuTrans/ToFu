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

import json
import os

import pytest

pytestmark = pytest.mark.unit

from lib.longform import recipe as rec
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
            return (json.dumps({'title': 'T', 'sections': ['A', 'B']}),
                    {'total_tokens': 10})
        return ('too short', {'total_tokens': 10})
    monkeypatch.setattr(rec, '_llm_chat', fake)
    with pytest.raises(st.StageFailed) as ei:
        rec.build_report_from_topic('x', str(tmp_path))
    assert ei.value.stage.startswith('section-')


# ══════════════════════════════════════════════════════════
#  THE MEASUREMENT — what the substrate did and didn't give us
# ══════════════════════════════════════════════════════════

def test_data_dependent_stage_list_rides_the_existing_resume_contract(
        monkeypatch, tmp_path):
    """The section stages don't exist until the outline does, so the recipe
    runs the graph twice against ONE checkpoint file. The second pass must
    SKIP research+outline from disk — proving the substrate's resume contract
    already covers a data-dependent stage list with no change to it."""
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
        'the second pass re-ran a completed stage — the resume contract does '
        'NOT cover data-dependent stage lists')


def test_crash_midway_resumes_without_redoing_finished_sections(
        monkeypatch, tmp_path):
    """A killed process must resume at the first UNWRITTEN section."""
    _patch_research(monkeypatch)
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

    def make_failing(index, heading):
        stage = orig_make(index, heading)
        if heading != 'C':
            return stage
        return st.Stage(stage.name, lambda ctx: (_ for _ in ()).throw(
            RuntimeError('killed')), gate=stage.gate, retry=0)

    monkeypatch.setattr(rec, '_make_section_stage', make_failing)
    with pytest.raises(st.StageFailed):
        rec.build_report_from_topic('x', str(tmp_path))
    assert written == ['A', 'B']

    # Second pass with C healthy: A and B must NOT be rewritten.
    written.clear()
    monkeypatch.setattr(rec, '_make_section_stage', orig_make)
    out = rec.build_report_from_topic('x', str(tmp_path))
    assert written == ['C'], f'resumed pass re-wrote {written} (should be only C)'
    assert out['sections'] == 3


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
                      desktop_enabled=False, swarm_enabled=False)
    names = {t['function']['name'] for t in assemble_tool_list(ctx)[0]}
    assert 'produce_report' in names
    assert 'produce_video' in names
