"""Production project workspace against an isolated local HTTP server/Chromium.

Exercises real kit controls, responsive styles, keyboard navigation and ordinary
conversation navigation. Server authority is covered by project/API tests.
"""
from pathlib import Path
import sys

import shutil
import subprocess
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_coordinator_graph_pages_reach_old_workers_with_bounded_dom(frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    entry = tmp_path / 'history-entry.ts'
    entry.write_text(f"export {{ createProjectCoordinationSurface }} from '{ROOT}/frontend/src/features/project-coordination-surface';\nexport {{ ready }} from '{ROOT}/frontend/src/i18n';\n")
    page.add_script_tag(path=native_module_path('.native/coordinator-history.js', entry))
    page.evaluate('async () => { await ready(); }')
    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/33-project-coordination.css'))
    page.evaluate('''async () => {
      document.body.innerHTML='<aside id="sidebar"></aside><main id="canvas"></main>';
      document.body.style.display='block';
      const binding={projectPath:'/project',conversationId:'leader',revision:1,availability:'available',capabilities:{assign:false,sendMessage:false,steerSubagent:false}};
      window.navigation=[];
      const scope={path:'/project',currentConversationId:'leader',currentPath:()=>'/project',
        binding:async()=>binding,board:async()=>({active:[],recentOutcomes:[]}),
        navigate:id=>window.navigation.push(id),bind:async()=>binding,openEvidence:()=>{},
        dispatch:{list:async(_path,_command,options)=>{
          const offset=Number(options?.cursor || 0);
          window.historyOffset=offset;
          return {olderCursor:offset<128?String(offset+32):null,newerCursor:offset?String(offset-32):null,
            assignments:Array.from({length:32},(_,index)=>({assignmentId:String(160-offset-index),
              request:{projectPath:'/project',coordinatorId:'leader',recipientId:'worker-'+(160-offset-index),brief:index===0 && offset===0 ? 'Coordinate the project update / 汇总每个 Worker 的结果与证据。'.repeat(6) : 'Worker '+(160-offset-index),
                authorizationSource:{conversationId:'leader',turnId:'human',projectionRevision:1},model:'test',budget:{maxTokens:1000,maxModelRequests:2,deadline:9999999999}},
              grantRevision:1,revoked:false,canonicalDelivery:false,review:null,
              execution:{turnId:'result',attemptId:'attempt',taskId:'task',status:offset?'completed':'running',resultRevision:1,result:{content:'A long Worker result with an error code and enough detail to require disclosure. '.repeat(12)}}}))};
        }}};
      window.historySurface=createProjectCoordinationSurface({sidebar:document.querySelector('#sidebar'),canvas:document.querySelector('#canvas'),
        scope:()=>scope,sidebarPath:()=>'/project',details:()=>{},create:()=>{}});
      await window.historySurface.refresh();
    }''')
    graph = page.locator('.coordination-task-graph')
    assert graph.locator('[data-node-kind=task]').count() == 32
    # Fixed graph geometry must contain long titles and summaries even when
    # the canvas is nested inside the real conversation summary shell.
    assert graph.locator('[data-node-id="assignment:160"]').evaluate('''node => {
      const bounds = node.getBoundingClientRect();
      return [...node.children, node.querySelector('.coordination-node-title')].every(child => {
        const rect = child.getBoundingClientRect();
        return rect.left >= bounds.left && rect.right <= bounds.right + 1
          && rect.top >= bounds.top && rect.bottom <= bounds.bottom + 1;
      });
    }''')
    for offset in (32, 64, 96, 128):
        page.locator('[data-coordination-focus=history-older]').click()
        page.locator(f'[data-node-id="assignment:{160-offset}"]').wait_for()
        assert graph.locator('[data-node-kind=task]').count() == 32
    assert page.locator('[data-coordination-focus=history-older]').is_disabled()
    assert graph.locator('[data-node-id="assignment:1"]').count() == 1
    for theme in ('dark', 'light', 'tofu'):
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        assert not page.locator('#canvas').evaluate('node=>node.scrollWidth > node.clientWidth + 1')
        output = Path('/tmp/tofu-coordinator-history-evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / f'{theme}-{width}.png'), full_page=True)
    page.locator('[data-coordination-focus=history-newer]').click()
    page.locator('[data-node-id="assignment:64"]').wait_for()
    page.locator('[data-coordination-focus=history-latest]').click()
    page.locator('[data-node-id="assignment:160"]').wait_for()
    graph.locator('[data-node-id="assignment:160"] button').click()
    inspector = page.locator('.coordination-node-inspector')
    assert inspector.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
    assert not graph.evaluate('node => node.scrollWidth > node.clientWidth + 1')
    page.screenshot(path=str(output / f'long-text-{width}.png'))
    inspector.locator('button').last.click()
    assert page.evaluate('window.navigation') == ['worker-160']
    page.evaluate('window.historySurface.destroy()')


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_project_workspace_attention_team_and_conversation_navigation(frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    # Supply the same CJK fallback explicitly on hosts whose system fontconfig
    # cannot expose installed variable fonts to the isolated Chromium build.
    cjk = Path(sys.prefix) / 'fonts/NotoSansCJKsc-VF.ttf'

    if not cjk.is_file() and shutil.which('fc-match'):
        font = subprocess.run(['fc-match', '-f', '%{file}', 'Noto Sans CJK SC'],
                              check=True, capture_output=True, text=True).stdout.strip()
        cjk = Path(font)
    if cjk.is_file():
        page.route('**/__workspace_cjk.ttf', lambda route: route.fulfill(path=str(cjk), content_type='font/ttf'))
        page.add_style_tag(content="@font-face{font-family:WorkspaceCJK;src:url('/__workspace_cjk.ttf')} :root{--font-ui:WorkspaceCJK,system-ui,sans-serif}")

    entry = tmp_path / 'workspace-entry.ts'
    entry.write_text(f"export {{ openProjectCoordination }} from '{ROOT}/frontend/src/features/project-coordination';\nexport {{ ready }} from '{ROOT}/frontend/src/i18n';\n")
    page.add_script_tag(path=native_module_path('.native/project-coordination.js', entry))
    page.evaluate('async () => { await ready(); }')
    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/33-project-coordination.css'))
    page.evaluate('''() => {
      window.navigation = []; window.subscriptions = 0;

      const task=(id,recipientId,brief,status,dependencies=[])=>({assignmentId:id,revoked:false,
        canonicalDelivery:false,grantRevision:1,
        request:{projectPath:'/workspace/tofu',coordinatorId:'coordinator',recipientId,brief,dependencies},
        dependencyStates:dependencies.map(reference=>({reference,status:'ready',code:'accepted',executionId:reference.assignmentId})),
        execution:{status,resultRevision:1},review:null});
      window.assignments=[task('search','research','调研与方案 · Research','running'),
        task('ui','frontend','前端工作台 · Workspace','queued',[{assignmentId:'done',grantRevision:1,resultRevision:1,predicate:'accepted'}]),
        task('failure','validation','集成验证 · Integration','failed'),
        task('done','backend','团队通信 · Team messaging','completed')];
      const binding = { projectPath:'/workspace/tofu', conversationId:'coordinator', revision:1,
        availability:'available', capabilities:{assign:false,sendMessage:false,steerSubagent:true} };
      window.ports = { path:'/workspace/tofu', currentConversationId:'coordinator', currentPath:()=>'/workspace/tofu',
        binding:async()=>binding, bind:async()=>binding,
        dispatch: { recipients:()=>[], humanTurns:()=>[], commandId:()=> 'command',
          list:async()=>({assignments:window.assignments}) },
        board:async()=>({active:[
          {id:'search',conversationId:'research',title:'调研与方案 · Research',status:'active',resultSummary:'正在核对证据与接口约束'},
          {id:'ui',conversationId:'frontend',title:'前端工作台 · Workspace',status:'active',changedPaths:['frontend/src/features/project-coordination.ts']}
        ],recentOutcomes:[
          {id:'failure',conversationId:'validation',title:'集成验证 · Integration',status:'failed',resultSummary:'测试环境缺少依赖，等待补齐'},
          {id:'done',conversationId:'backend',title:'团队通信 · Team messaging',status:'completed',resultSummary:'消息链路已实现，等待交付检查'}
        ]}),
        conversations:()=>[{id:'research',title:'Research worker'},{id:'frontend',title:'Frontend worker'},{id:'backend',title:'Backend worker'}],
        subscribe:cb=>{window.refreshWorkspace=cb; window.subscriptions++; return ()=>window.subscriptions--;},
        navigate:id=>window.navigation.push(id), openEvidence:()=>{},
      };
      window.workspace = openProjectCoordination(window.ports);
    }''')
    dialog = page.locator('dialog[open]')
    dialog.locator('.coordination-task-graph').wait_for()
    assert 'projectCoordination.' not in dialog.inner_text()
    page.evaluate('async () => { await document.fonts.ready; }')
    assert dialog.get_by_role('tabpanel').count() == 1
    assert 'Integration' in dialog.locator('[data-status=failed]').inner_text()
    assert dialog.locator('.coordination-metrics').count() == 0
    assert dialog.locator('[data-status=running]').count() == 1
    assert dialog.locator('[data-status=queued]').count() == 1
    assert dialog.locator('.coordination-graph-node[data-node-kind=task]').count() == 4
    assert dialog.locator('svg [data-edge-kind=dependency]').count() == 3
    assert dialog.locator('.coordination-group').count() == 0
    for theme in ('dark', 'light', 'tofu'):
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        assert not dialog.evaluate('node=>node.scrollWidth > node.clientWidth + 1')
        assert dialog.bounding_box()['width'] <= width
        output = ROOT / 'designs/project-coordination/previews/workspace'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'{theme}-{width}.png'), full_page=True)
    # Completed prerequisites remain visible; the inspector reveals evidence.
    dialog.locator('[data-node-id="assignment:ui"] button').click()
    assert '团队通信' in dialog.locator('.coordination-node-inspector').inner_text()
    # Tabs must hide inactive ui-stack panels, retain keyboard navigation and
    # keep advanced mutation controls off the overview.
    tabs = dialog.get_by_role('tab')
    tabs.nth(0).focus(); page.keyboard.press('ArrowRight')
    assert tabs.nth(1).get_attribute('aria-selected') == 'true'
    assert dialog.get_by_role('tabpanel').count() == 1
    assert not dialog.locator('.coordination-task-graph').is_visible()
    panel = dialog.get_by_role('tabpanel')
    assert 'Research worker' in panel.inner_text()
    # Project events arriving while another tab is open are read on return.
    # Real browser focus must survive both a failed background read and a
    # successful changed snapshot; unchanged snapshots keep the original node.
    page.evaluate('''() => {
      window.assignments[0].request.brief='Updated research';
      window.assignments[1].execution.status='running';
      window.refreshWorkspace();
    }''')
    tabs.nth(0).click()
    page.wait_for_function("document.querySelector('.coordination-task-graph').textContent.includes('Updated research')")
    assert dialog.locator('[data-status=running]').count() == 2
    action = dialog.locator('[data-status=running]').first.get_by_role('button')
    action.focus()
    page.evaluate('window.focusedAction=document.activeElement')
    page.evaluate('''() => {
      window.originalBinding=window.ports.binding;
      window.ports.binding=async()=>{throw new Error('Temporary background failure');};
      window.refreshWorkspace();
    }''')
    dialog.get_by_text('Temporary background failure').wait_for()
    assert page.evaluate('document.activeElement === window.focusedAction')
    assert action.is_visible()
    page.evaluate('''() => {
      window.ports.binding=window.originalBinding;
      window.assignments[0].request.brief='Research refreshed';
      window.refreshWorkspace();
    }''')
    page.wait_for_function("!document.body.textContent.includes('Temporary background failure')")
    assert action.evaluate('node=>node===document.activeElement')
    tabs.nth(1).click()
    assert 'Research worker' in panel.inner_text()
    panel.locator('article').first.get_by_role('button').click()
    page.wait_for_function('window.navigation.length === 1 && window.subscriptions === 0')
    assert page.evaluate('window.navigation[0]') == 'research'
    assert page.locator('dialog').count() == 0
    # Return through the same project entrance to the coordinator canvas.
    page.evaluate('() => { window.ports.currentConversationId="research"; window.workspace=openProjectCoordination(window.ports); }')
    page.locator('.coordination-entry').get_by_role('button').first.click()
    page.wait_for_function('window.navigation.length === 2')
    assert page.evaluate('window.navigation[1]') == 'coordinator'


def test_large_coordinator_graph_keeps_dom_and_viewport_bounded(frontend_visual_page, tmp_path):
    """Measure real layout at the assignment boundary; no model or API calls."""
    import json
    page = frontend_visual_page
    page.set_viewport_size({'width': 1440, 'height': 1000})
    entry = tmp_path / 'graph-entry.ts'
    entry.write_text(f"export {{ createCoordinationBoard }} from '{ROOT}/frontend/src/features/project-coordination-board';\n"
                     f"export {{ ready }} from '{ROOT}/frontend/src/i18n';\n")
    page.add_script_tag(path=native_module_path('.native/coordinator-large-graph.js', entry))
    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/33-project-coordination.css'))
    result = page.evaluate('''async () => {
      await ready();
      const assignments = Array.from({length:128}, (_,i) => ({assignmentId:String(i),
        grantRevision:1, revoked:false, canonicalDelivery:false, review:null,
        request:{coordinatorId:'coordinator',recipientId:'worker-'+i,brief:'Worker task '+i,
          dependencies:Array.from({length:Math.min(i,8)},(_,j)=>({assignmentId:String(i-j-1),grantRevision:1,resultRevision:1}))},
        execution:{status:'running',resultRevision:1,result:{content:'Verified evidence. '.repeat(50)}}
      }));
      const start = performance.now();
      const graph = createCoordinationBoard({active:[],recent:[]},()=>{},0,{assignments,coordinatorId:'coordinator'});
      document.body.append(graph);
      const viewport = graph.querySelector('.coordination-graph-viewport');
      const bounds = viewport.getBoundingClientRect();
      return {layoutMs:performance.now()-start, nodes:graph.querySelectorAll('[data-node-id]').length,
        elements:graph.querySelectorAll('*').length, viewportHeight:bounds.height,
        visibleWireLayers:[...graph.querySelectorAll('svg')].filter(el=>getComputedStyle(el).display!=='none').length};
    }''')
    assert result['nodes'] == 129
    assert result['elements'] < 5000
    assert result['visibleWireLayers'] == 1
    assert result['viewportHeight'] <= 480
    (tmp_path / 'graph-performance.json').write_text(json.dumps(result, indent=2))
    print('coordinator graph measurement:', json.dumps(result, sort_keys=True))
