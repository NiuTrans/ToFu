"""Auto-research reuses the project's Agent event/UI contracts end to end."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_VIEW_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'research-view.ts')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


@pytest.mark.parametrize('raw,expected', [
    ('2502.00299v5', '2502.00299'),
    ('arXiv:2502.00299v5', '2502.00299'),
    ('https://arxiv.org/abs/2502.00299v5', '2502.00299'),
    ({'arxiv_id': 'hep-th/0601001v2'}, 'hep-th/0601001'),
])
def test_shared_arxiv_normalizer_handles_model_citation_shapes(raw, expected):
    from lib.paper.arxiv import normalize_arxiv_id
    assert normalize_arxiv_id(raw) == expected


def test_recipe_threads_tool_events_through_survey_and_ideate(monkeypatch):
    import lib.research.recipe as rc

    seen = []
    gaps = {'open_gaps': [{'id': 'g1'}]}

    def survey(*args, on_tool_event=None, **kwargs):
        assert callable(on_tool_event)
        on_tool_event({'type': 'tool_start', 'toolName': 'web_search'})
        return {'ok': True, 'open_gaps': gaps, 'survey_md': '# S',
                'inputs_used': 3, 'citation_audit': None,
                'usage': {'calls': 2}}

    def ideate(*args, on_tool_event=None, **kwargs):
        assert callable(on_tool_event)
        on_tool_event({'type': 'tool_done', 'toolName': 'web_search'})
        return {'ok': True, 'accepted': [],
                'rejected': [{'reject_stage': 'rubric'}], 'threshold': 4.0,
                'gate_reached': 'rubric', 'usage': {'calls': 3}}

    monkeypatch.setattr(rc, '_build_survey', survey)
    monkeypatch.setattr(rc, '_generate_ideas', ideate)
    monkeypatch.setattr(rc, '_persist_survey', lambda *a, **k: True)
    monkeypatch.setattr(rc, '_persist_ideate', lambda *a, **k: True)
    ctx = {'direction': 'd', 'lang': 'en', 'user_id': 1,
           'abort': lambda: False, 'n_ideas': 2, 'emit': seen.append,
           'artifacts': {'harvest': {'arxiv_ids': ['1', '2', '3'],
                                     'folder_id': 'f'}}}
    survey_art = rc._run_survey(ctx)
    ctx['artifacts']['survey'] = survey_art
    ideate_art = rc._run_ideate(ctx)

    assert [event['type'] for event in seen] == ['tool_start', 'tool_done']
    assert survey_art['usage']['calls'] == 2
    assert ideate_art['usage']['calls'] == 3


def test_survey_grounds_against_loaded_corpus_not_mutable_folder(monkeypatch):
    """A paper can be shared by multiple research runs but has one folder_id.
    Folder drift must not downgrade a paper the current survey actually read."""
    import lib.paper.survey as survey

    monkeypatch.setattr(survey, '_load_paper_inputs', lambda *a, **k: [{
        'arxiv_id': '2502.00299', 'paper_hash': 'h', 'title': 'ChunkKV',
        'source': 'parsed_text', 'content': 'paper text'}])
    monkeypatch.setattr(survey, '_synthesize_survey', lambda *a, **k: (
        '# Survey arXiv:2502.00299',
        {'schema_version': 1, 'clusters': [], 'method_matrix': [],
         'open_gaps': [{'id': 'g1', 'gap': 'x',
                        'evidence': ['2502.00299']}]}))
    monkeypatch.setattr(survey, '_audit_citations', lambda *a, **k: None)
    # If build_survey falls back to folder membership, this empty set makes the
    # evidence grounded/low-confidence (or stripped) instead of library-grade.
    monkeypatch.setattr(survey, '_library_id_set', lambda *a, **k: set())

    got = survey.build_survey('direction', ['2502.00299'], folder_id='raced-away')
    gap = got['open_gaps']['open_gaps'][0]
    assert gap['evidence_tiers']['2502.00299'] == 'library'
    assert gap['library_evidence_count'] == 1
    assert gap['low_confidence'] is False
    assert got['open_gaps']['surveyed_arxiv_ids'] == ['2502.00299']


@pytest.mark.skipif(not shutil.which('node') or not os.path.isdir(
    os.path.join(ROOT, 'node_modules', 'jsdom')) or not os.path.isfile(ESBUILD),
    reason='node/jsdom/esbuild dev-deps not installed')
def test_frontend_replays_agent_tools_and_renders_usage_accounting(tmp_path):
    harness = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1];
const {JSDOM} = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<body><div id="paperPdfViewer"></div></body>',
                      {url:'http://localhost/'});
global.window=global; global.document=dom.window.document;
global.escapeHtml=(s)=>String(s == null ? '' : s);
global.t=(k)=>({
  'paper.research.usageSummary':'{calls} calls · {input} input · {output} output · {cost}',
  'paper.research.usageLine':'{calls} calls · {input} input · {output} output · {cache} cached'
}[k] || k); global.renderMarkdown=(x)=>x;
global.renderToolRoundsHTML=(rows)=>'<div data-agent-tools="1">'+rows.length+'</div>';
global.pushSubscribe=()=>{}; global.pushUnsubscribe=()=>{};
global.Api={research:{lookup:async()=>({found:false})}};
(0,eval)(fs.readFileSync(process.argv[2],'utf8'));
const st=_newResearchStream('direction');
_researchApplySnapshot(st,{status:'done',result:{accepted:[],rejected:[{}],corpus_size:3,
  evaluation:{overall_score:4.25,worth_following_up:true,judge_count:2,
    consensus:'unanimous',scores:{survey_coverage:4.0},
    failure_modes:['thin_matrix'],recommended_changes:[{priority:'high',change:'Require matrix rows'}],
    verdict:'Promising but the comparison matrix is thin.'},
  usage:{total:{calls:5,prompt_tokens:1400,completion_tokens:240,priced_calls:5,
                cost_cny:0.42,cost_estimated:false},
         stages:{survey:{calls:2,prompt_tokens:1000,completion_tokens:100,cache_read_tokens:600},
                 ideate:{calls:1,prompt_tokens:200,completion_tokens:100,cache_read_tokens:0},
                 evaluate:{calls:2,prompt_tokens:200,completion_tokens:40,cache_read_tokens:0}}}},
  events:[
    {seq:1,type:'stage_started',stage:'survey'},
    {seq:2,type:'tool_start',roundNum:0,toolName:'web_search',toolCallId:'c1',query:'q'},
    {seq:3,type:'tool_done',roundNum:0,toolName:'web_search',toolCallId:'c1',elapsed:1.2}
  ]});
_researchStream=st; _paintResearch();
console.log(JSON.stringify({phase:st.phase,rounds:st.toolRounds,
  html:document.getElementById('paperPdfViewer').innerHTML}));
"""
    built = native_module_path('paper/research-view.js', RESEARCH_VIEW_TS)
    proc = subprocess.run(['node', '-e', harness, ROOT, str(built)], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got['phase'] == 'survey'
    assert len(got['rounds']) == 1 and got['rounds'][0]['status'] == 'done'
    assert 'data-agent-tools="1"' in got['html']
    assert 'paper.research.usageTitle' in got['html']
    assert '1,400' in got['html']
    assert '4.25 / 5' in got['html']
    assert 'thin_matrix' in got['html']
    assert 'Require matrix rows' in got['html']
