"""Real browser reattaches owned actions without starting work or losing drafts."""
from pathlib import Path
import subprocess
import pytest
from tests._runtime_sections import _require_frontend_bundler
from tests.e2e.test_e2e_smoke import _wait_app_ready
from lib.research.workspace import empty_workspace

ROOT=Path(__file__).resolve().parents[2]
pytestmark=pytest.mark.visual


@pytest.fixture(scope='module')
def rejoin_bundle(tmp_path_factory):
    _require_frontend_bundler();directory=tmp_path_factory.mktemp('workbench-rejoin')
    entry=directory/'entry.ts';entry.write_text(
        f'export * from "{ROOT}/frontend/src/features/evolution/research/controllers/workspace";\n'
        f'export {{ featureRegistry }} from "{ROOT}/frontend/src/feature-registry";\n')
    output=directory/'workspace.js'
    subprocess.run(['node',str(ROOT/'scripts/vite_test_bundle.mjs'),str(entry),'--bundle',
        '--format=iife','--global-name=RejoinWorkspace',f'--outfile={output}'],cwd=ROOT,
        check=True,capture_output=True,text=True)
    return output


@pytest.mark.parametrize('mode',['running','pending','ambiguous','unavailable'])
def test_reopen_tracks_unique_action_and_blocks_uncertain_launch(page,rejoin_bundle,mode,assert_no_js_errors):
    _wait_app_ready(page);page.add_script_tag(path=str(rejoin_bundle))
    workspace=empty_workspace('Long owned action','en');workspace['hypothesis']='Original hypothesis'
    page.evaluate('''async ({workspace,mode})=>{
      const r=RejoinWorkspace, registry=r.featureRegistry;
      registry._researchStream={direction:workspace.direction,lang:'en',accepted:[],rejected:[]};
      window.rejoinReads=0;window.rejoinStarts=0;window.rejoinLists=[];
      const task={id:'original-task',kind:'research-action',status:mode==='pending'?'pending':'running',
        meta:{direction:workspace.direction,lang:'en',action:'analyze'}};
      registry.Api={research:{workspace:async()=>({workspace}),
        capabilities:async()=>({catalog:{tools:[],capabilities:[]}})},tasks:{
        list:async(kind,status,filters)=>{window.rejoinLists.push({kind,status,filters});
          if(mode==='unavailable')return null;
          return {tasks:status===task.status?(mode==='ambiguous'?[task,{...task,id:'other-task'}]:[task]):[]};},
        get:async()=>{window.rejoinReads++;return new Promise(resolve=>window.resolveRejoin=resolve);},
        start:async()=>{window.rejoinStarts++;return {taskId:'duplicate'};},
      }};
      document.querySelectorAll('#researchWorkspace').forEach(node=>node.remove());
      const host=document.createElement('main');host.id='researchWorkspace';document.body.append(host);
      await r.loadResearchWorkspace(workspace.direction,'en');
      await r.loadResearchWorkspace(workspace.direction,'en');
      await r.startResearchAction('manuscript');
    }''',{'workspace':workspace,'mode':mode})
    assert page.evaluate('window.rejoinStarts')==0
    assert page.evaluate('window.rejoinLists.every(row=>row.filters.direction==="Long owned action"&&row.filters.lang==="en"&&row.filters.limit===2)')
    assert page.locator('#researchWorkspace .rsw-action').evaluate_all('nodes=>nodes.every(node=>node.disabled)')
    if mode in ('running','pending'):
        assert page.evaluate('window.rejoinReads')==1
        assert page.locator('#researchWorkspace [data-tofu-action="_abortResearchAction()"] ').is_visible()
        field=page.locator('#researchWorkspace [data-rsw-field="hypothesis"]')
        field.fill('Unsaved user edit')
        page.evaluate('window.resolveRejoin({status:"done",result:{workspace:{revision:2,hypothesis:"Server result"}}})')
        page.wait_for_function('!document.querySelector("#researchWorkspace [data-tofu-action=\\"_abortResearchAction()\\"]")')
        assert field.input_value()=='Unsaved user edit'
    else:
        assert page.evaluate('window.rejoinReads')==0
        assert page.locator('#researchWorkspace .rs-alert.is-error').is_visible()
    page.evaluate('RejoinWorkspace.destroyResearchWorkspace()')


def test_late_lookup_cannot_reattach_another_workspace(page,rejoin_bundle,assert_no_js_errors):
    _wait_app_ready(page);page.add_script_tag(path=str(rejoin_bundle))
    result=page.evaluate('''async template=>{
      const r=RejoinWorkspace, registry=r.featureRegistry;
      registry._researchStream={direction:'First',lang:'en',accepted:[],rejected:[]};
      let release;let gets=0;
      registry.Api={research:{workspace:async direction=>({workspace:{...structuredClone(template),direction}}),
        capabilities:async()=>({catalog:{tools:[],capabilities:[]}})},tasks:{
        list:async(_kind,status,filters)=>filters.direction==='First'&&status==='running'
          ?new Promise(resolve=>release=resolve):{tasks:[]},
        get:async()=>{gets++;return {status:'running'};},
      }};
      document.querySelectorAll('#researchWorkspace').forEach(node=>node.remove());
      const host=document.createElement('main');host.id='researchWorkspace';document.body.append(host);
      const old=r.loadResearchWorkspace('First','en');
      await Promise.resolve();
      await r.loadResearchWorkspace('Second','en');
      release({tasks:[{id:'old-task',kind:'research-action',status:'running',
        meta:{direction:'First',lang:'en',action:'analyze'}}]});
      await old;
      const attached=!!host.querySelector('[data-tofu-action="_abortResearchAction()"]');
      r.destroyResearchWorkspace();return {gets,attached};
    }''',empty_workspace('First','en'))
    assert result=={'gets':0,'attached':False}
