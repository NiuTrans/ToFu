"""Decision/evidence rendering exercises shipped TypeScript under jsdom."""
import json
import subprocess

import pytest
from tests._paper_vite import compiled_typescript
from tests.frontend.test_frontend_research_entry import ROOT, RESEARCH_VIEW_TS, requires_node, _HARNESS

pytestmark = pytest.mark.unit


@requires_node
def test_research_decisions_real_payload(tmp_path):
    prefix = _HARNESS.split('(async () => {')[0]
    scenario = r'''
const t = () => '';
const cycle = {status:'budget_exhausted',revision_count:2,next_action:'Test <img src=x onerror=alert(1)>',
 issues:[{idea_id:'idea_A',change:'Add baseline <script>x</script>',target:'ideate',priority:'high',status:'unresolved',evidence:'Missing ablation'}],
 history:[{revision:0,ideas:{accepted:[{title:'Old <b>idea</b>',idea_version:1,core_mechanism:'Old mechanism'}]}}]};
const payload = {accepted:[{title:'Current idea',idea_version:2,overall:null,scores:{novelty:0,value:null}}],
 open_gaps:{open_gaps:[{title:'Named gap',rationale:'Why <script>x</script>'},{description:'Description gap'},{gap:{bad:true},why_open:{bad:true}}]},
 research_cycle:cycle,corpus_arxiv_ids:['1234.5678'],corpus_size:1};
const stream = _newResearchStream('Test');
_researchApplySnapshot(stream,{status:'done',result:payload});
global._researchStream=stream;
_paintResearch();
const html=document.getElementById('researchViewer').innerHTML;
const scores=_researchScoresHtml({novelty:0,value:null,other:'',bad:false},null,t);
const absent=_researchScoresHtml(null,null,t);
const zero=_researchScoresHtml(null,0,t);
const legacy=_newResearchStream('Legacy');
_researchApplySnapshot(legacy,{status:'done',result:{accepted:[]}});
const restored=_newResearchStream('Restore');
_researchApplyArtifacts(restored,payload);
_researchApplyArtifacts(restored,{evaluation:{verdict:'older optional payload'}});
const live=_newResearchStream('Live');
_researchApplyEvent(live,{type:'final',result:payload});
console.log(JSON.stringify({html,scores,absent,zero,legacy:legacy.researchCycle||null,
 restored:restored.researchCycle,live:live.researchCycle,liveIdeas:live.acceptedIdeas}));
process.exit(0);
'''
    harness = tmp_path / 'decisions.js'
    harness.write_text(prefix + scenario)
    with compiled_typescript(RESEARCH_VIEW_TS, expose_feature_registry_to_window=True) as built:
        result = subprocess.run(['node', str(harness), ROOT, 'clean', built], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    html = out['html']
    assert 'Named gap' in html and 'Description gap' in html
    assert 'Gap description unavailable' in html or 'evolution.research.gapMissing' in html
    assert '[object Object]' not in html
    assert '<script>' not in html and '<img src=x' not in html
    assert '&lt;script&gt;' in html and '&lt;img' in html
    assert 'Current idea' in html and 'Old &lt;b&gt;idea&lt;/b&gt;' in html
    assert 'Missing ablation' in html and 'idea_A' in html
    assert '0.00' not in html
    assert out['absent'] == ''
    assert '0.00' in out['zero']
    assert '>0.0<' in out['scores'] and 'Value' not in out['scores']
    assert out['legacy'] is None
    assert out['restored']['revision_count'] == 2
    assert out['live']['status'] == 'budget_exhausted'
    assert out['liveIdeas'][0]['idea_version'] == 2
