"""Backend-ordered round sections and mounted, reader-owned disclosure behavior."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def round_bundle(tmp_path_factory):
    built = tmp_path_factory.mktemp('round-outline') / 'round.cjs'
    result = subprocess.run([
        str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/conversation/index.ts'), '--bundle',
        '--format=cjs', '--platform=node', f'--outfile={built}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return built


def run_js(round_bundle, body):
    source = f'''
const {{JSDOM}}=require('jsdom');
const dom=new JSDOM('<!doctype html><body></body>');
global.document=dom.window.document; global.window=dom.window;
Object.assign(global,require({json.dumps(str(round_bundle))}));
console.log((function(){{ {body} }})());
'''
    result = subprocess.run([shutil.which('node'), '-e', source], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


HELPERS = r'''
const block=(id,round,attempt='a',kind='thinking',extra={})=>({
  kind,blockId:id,ordinal:1,terminal:false,markdown:id,text:id,
  deliverable:false,source:{llmRound:round,attemptId:attempt,taskId:attempt},...extra
});
const turn=(blocks,status='completed')=>({turnId:'t',presentationId:'t',actor:'assistant',role:'assistant',
  laneId:'main',ordinal:1,status,terminal:status==='completed',blocks,
  queuedTurns:[],branches:[],actions:[],metadata:{translation:{pending:false,streaming:false}},source:{},finish:null});
const vm=t=>({conversationId:'c',conversationRevision:1,transport:'ready',
  mainLane:{laneId:'main',title:'Main',kind:'main',expanded:true,turns:[t]},
  orphanLanes:[],queue:[],planDecision:null,transportNotice:null});
'''


def test_outline_uses_explicit_scope_and_segments_not_inspector_or_diagnostics(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const blocks=[block('one',0), block('two',0),
        block('diagnostic',0,'a','activity'), block('continued',0),
        block('resumed',0,'b'), block('answer',0,'b','text',{deliverable:true}),
        block('legacy',null), block('invalid',-1),
        block('note',0,'b','system-note')];
      const sections=selectRoundOutline(turn(blocks));
      const scoped=block('scoped',1,'new','tool',{round:{llmRound:99,attemptId:'old',status:'done'}});
      return JSON.stringify({
        blocks:sections.map(s=>s.blockIds),scopes:sections.map(s=>s.identity.attemptId),
        rounds:sections.map(s=>s.identity.llmRound),
        scope:selectRoundOutline(turn([scoped]))[0].identity.attemptId,
        invalid:[{}, {llmRound:-1}, {llmRound:1.5}, {llmRound:'bad'},
          {roundNum:10}].map(roundIdentityOf),
        legacy:roundIdentityOf({_round:{llmRound:'2',_attemptId:'x',_taskId:'y'}})
      });
    ''')
    assert result == {
        'blocks': [['one', 'two'], ['continued'], ['resumed']],
        'scopes': ['a', 'a', 'b'], 'rounds': [0, 0, 0], 'scope': 'new',
        'invalid': [None] * 5,
        'legacy': {'llmRound': 2, 'attemptId': 'x', 'taskId': 'y', 'key': '["x","y",2]'},
    }


def test_outline_states_do_not_treat_background_work_as_completed(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const tool=(id,n,round)=>block(id,n,'a','tool',{round:{toolName:'read_files',...round}});
      const blocks=[
        tool('routine',0,{status:'done'}),
        tool('background',1,{status:'done',results:[{backgrounded:true,exitCode:'background'}]}),
        tool('failed',2,{status:'done',results:[{exitCode:1}]}),
        tool('active',3,{status:'running'}),
        tool('settled',4,{status:'done',results:[{backgrounded:true,backgroundCompleted:true,exitCode:0}]}),
        block('program',5,'a','program',{source:{llmRound:5,status:'done',result:{backgrounded:true}}}),
      ];
      return JSON.stringify(selectRoundOutline(turn(blocks)).map(s=>({
        state:s.state,open:s.defaultExpanded,background:s.backgroundCount,tools:s.toolCount
      })));
    ''')
    assert result == [
        {'state': 'returned', 'open': False, 'background': 0, 'tools': 1},
        {'state': 'background', 'open': True, 'background': 1, 'tools': 1},
        {'state': 'attention', 'open': True, 'background': 0, 'tools': 1},
        {'state': 'active', 'open': True, 'background': 0, 'tools': 1},
        {'state': 'returned', 'open': False, 'background': 0, 'tools': 1},
        {'state': 'background', 'open': True, 'background': 1, 'tools': 1},
    ]


def test_outline_controls_preserve_nodes_focus_reader_choice_and_answer(round_bundle):
    """Action blocks keep their summary visible; toolbar operates evidence, not shells."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const surface=createConversationSurface(root);
      let blocks=[block('done',0,'a','tool',{round:{toolName:'read_files',status:'done'}}),block('live',1,'a','tool',{round:{toolName:'read_files',status:'running'}}),
        block('answer',1,'a','text',{deliverable:true})];
      surface.render(vm(turn(blocks,'running')));
      // No round headers: each action owns its summary at its authored position.
      const noHeaders=root.querySelectorAll('.conversation-round-header').length===0;
      const doneBlock=root.querySelector('[data-block-id="done"]');
      const liveBlock=root.querySelector('[data-block-id="live"]');
      const answerBlock=root.querySelector('[data-block-id="answer"]');
      const allVisible=[doneBlock,liveBlock,answerBlock].every(n=>!n.hidden);
      // Tool blocks carry action state.
      const states=[...root.querySelectorAll('[data-action-state]')].map(n=>n.dataset.actionState);
      // No toolbar with <4 actions.
      const toolbarHidden=root.querySelector('.conversation-round-toolbar').hidden;
      // Replay preserves keyed nodes and reader state.
      const leaf=root.querySelector('[data-block-id="done"]');
      for(let i=0;i<20;i++) surface.render(vm(turn(JSON.parse(JSON.stringify(blocks)),'running')));
      const retained=leaf===root.querySelector('[data-block-id="done"]');
      const order=[...root.querySelectorAll('.conversation-block')].map(n=>n.dataset.blockId);
      // Clone determinism.
      const clone=document.createElement('main');document.body.append(clone);
      const fresh=createConversationSurface(clone);fresh.render(vm(turn(blocks,'running')));
      const semantic=(node)=>[...node.querySelectorAll('[data-action-state]')].map(n=>[n.dataset.blockId,n.dataset.actionState]);
      const same=JSON.stringify(semantic(root))===JSON.stringify(semantic(clone));
      // Removing actions removes blocks, not headers.
      surface.render(vm(turn([blocks[2]],'completed')));
      const removed=root.querySelectorAll('[data-action-state]').length===0;
      surface.dispose();fresh.dispose();
      return JSON.stringify({noHeaders,allVisible,states,toolbarHidden,retained,order,same,removed});
    ''')
    assert result == {
        'noHeaders': True, 'allVisible': True,
        'states': ['returned', 'active'],
        'toolbarHidden': True,
        'retained': True, 'order': ['done', 'live', 'answer'],
        'same': True, 'removed': True,
    }


def test_live_to_terminal_defaults_change_without_overriding_reader(round_bundle):
    """Action state transitions are visible on the block, not a heading wrapper."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const surface=createConversationSurface(root);
      const blocks=[block('thinking',0,'a','tool',{round:{toolName:'read_files',status:'running'}})];
      surface.render(vm(turn(blocks,'running')));
      const node=root.querySelector('[data-block-id="thinking"]');
      const liveState=node.dataset.actionState;
      // The minimal test renderer replaces children; check the dataset that
      // drives the state indicator in the real renderer.
      const liveIndicator=node.dataset.actionState==='active';
      blocks[0].round.status='done';
      surface.render(vm(turn(blocks,'completed')));
      const settledState=node.dataset.actionState;
      const settledIndicator=node.dataset.actionState==='returned';
      const failed=block('thinking',0,'a','tool',{round:{status:'error',toolName:'read_files'}});
      surface.render(vm(turn([failed],'completed')));
      const attentionState=node.dataset.actionState;
      surface.dispose();return JSON.stringify({liveState,liveIndicator,settledState,settledIndicator,attentionState});
    ''')
    assert result == {
        'liveState': 'active', 'liveIndicator': True,
        'settledState': 'returned', 'settledIndicator': True,
        'attentionState': 'attention',
    }


def test_reasoning_terminal_is_a_tail_marker_not_a_completion_status(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const old=block('history',0,'a','thinking',{terminal:false});
      const tail=block('tail',1,'a','thinking',{terminal:true});
      const shape=(status)=>selectRoundOutline(turn([old,tail],status))
        .map(s=>[s.state,s.defaultExpanded]);
      return JSON.stringify({live:shape('running'),finished:shape('completed')});
    ''')
    assert result == {'live': [['returned', False], ['active', True]],
                      'finished': [['returned', False], ['returned', False]]}


def test_any_unresolved_background_result_keeps_a_round_open(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const tool=block('mixed',0,'a','tool',{round:{toolName:'read_files',status:'done',
        results:[{exitCode:0},{backgrounded:true,exitCode:'background'}]}});
      const pending=selectRoundOutline(turn([tool]))[0];
      tool.round.results[1]={backgrounded:true,backgroundCompleted:true,exitCode:0};
      const settled=selectRoundOutline(turn([tool]))[0];
      return JSON.stringify([pending.state,pending.defaultExpanded,settled.state]);
    ''')
    assert result == ['background', True, 'returned']


def test_round_controls_belong_to_the_surface_document_without_global_document(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const doc=document;
      delete globalThis.document;
      const root=doc.createElement('main');doc.body.append(root);
      const surface=createConversationSurface(root);
      surface.render(vm(turn([block('thinking',0,'a','tool',{round:{toolName:'read_files',status:'done'}})])));
      const buttons=[...root.querySelectorAll('button')];
      const result={count:buttons.length,owned:buttons.every(b=>b.ownerDocument===doc)};
      surface.dispose();return JSON.stringify(result);
    ''')
    assert result['count'] >= 4
    assert result['owned'] is True


def test_collapsed_rounds_render_on_reveal_with_latest_projection(round_bundle):
    """All blocks render immediately; replay preserves keyed nodes."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const calls=[];const surface=createConversationSurface(root,{renderBlock(node,block){
        calls.push(block.blockId);node.textContent=block.blockId;
      }});
      let blocks=[block('history',0,'a','tool',{round:{toolName:'read_files',status:'done'}}),block('answer',1,'a','text',{deliverable:true})];
      surface.render(vm(turn(blocks)));
      const shell=root.querySelector('[data-block-id="history"]');
      const initial={calls:[...calls],visible:!shell.hidden};
      // Replay with identical data: nodes retained, no re-render needed.
      surface.render(vm(turn(JSON.parse(JSON.stringify(blocks)))));
      const retained=shell===root.querySelector('[data-block-id="history"]');
      const stable=calls.length===2; // no unnecessary re-renders
      // New data triggers re-render.
      blocks[0]={...blocks[0],round:{...blocks[0].round,status:'running'}};
      surface.render(vm(turn(blocks)));
      const reRendered=calls.length>2;
      surface.dispose();
      return JSON.stringify({initial,retained,stable,reRendered});
    ''')
    assert result == {
        'initial': {'calls': ['history', 'answer'], 'visible': True},
        'retained': True, 'stable': True, 'reRendered': True,
    }


def test_content_is_visible_before_details_and_short_turn_has_no_false_history(round_bundle):
    """Prose and tool summaries are always visible; no false history controls."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const surface=createConversationSurface(root,{windowing:{maxBlocksPerTurn:12}});
      const blocks=[...Array.from({length:9},(_,i)=>block('note-'+i,0,'a','system-note')),
        block('prose',0,'a','text',{text:'Readable narration',markdown:'Readable narration'}),
        block('tool',0,'a','tool',{round:{toolName:'read_files',status:'done'}}),
        block('prose-2',1,'a','text'),block('tool-2',1,'a','tool',{round:{toolName:'read_files',status:'done'}})];
      surface.render(vm(turn(blocks)));
      const prose=root.querySelector('[data-block-id="prose"]');
      const result={proseVisible:!prose.hidden,
        toolVisible:!root.querySelector('[data-block-id="tool"]').hidden,
        prose2Visible:!root.querySelector('[data-block-id="prose-2"]').hidden,
        tool2Visible:!root.querySelector('[data-block-id="tool-2"]').hidden,
        noRoundHeaders:root.querySelectorAll('.conversation-round-header').length===0,
        history:!!root.querySelector('.conversation-block-window-control'),
        toolbarHidden:root.querySelector('.conversation-round-toolbar').hidden};
      surface.dispose();return JSON.stringify(result);
    ''')
    assert result == {'proseVisible': True, 'toolVisible': True,
                      'prose2Visible': True, 'tool2Visible': True,
                      'noRoundHeaders': True, 'history': False,
                      'toolbarHidden': True}


def test_program_summary_remains_visible_while_child_details_are_deferred(round_bundle):
    """Program and child actions each own their summary; all visible at authored positions."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const rendered=[];
      const surface=createConversationSurface(root,{renderBlock(node,block){
        rendered.push(block.blockId);node.textContent=block.text;
      }});
      const blocks=[block('program',0,'a','program',{round:{_programSynthetic:true,programStatus:'completed'}}),
        block('child',0,'a','tool',{round:{toolName:'web_search',status:'done'}}),
        block('answer',1,'a','text',{deliverable:true})];
      surface.render(vm(turn(blocks)));
      const program=root.querySelector('[data-block-id="program"]');
      const child=root.querySelector('[data-block-id="child"]');
      const result={programVisible:!program.hidden,childVisible:!child.hidden,
        rendered:[...rendered],noHeaders:root.querySelectorAll('.conversation-round-header').length===0};
      surface.dispose();return JSON.stringify(result);
    ''')
    assert result == {
        'programVisible': True, 'childVisible': True,
        'rendered': ['program', 'child', 'answer'],
        'noHeaders': True,
    }


def test_failed_swarm_launch_is_attention_not_an_unconfirmed_live_round(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const error=block('spawn',0,'a','tool',{round:{toolName:'spawn_agents',status:'done',
        toolContent:"Swarm tool error: ImportError: cannot import name 'observed_usage_totals'"}});
      const section=selectRoundOutline(turn([error]))[0];
      return JSON.stringify([section.state,section.defaultExpanded,section.backgroundCount]);
    ''')
    assert result == ['attention', True, 0]


def test_self_contained_tool_cards_never_join_a_round_disclosure(round_bundle):
    """Every action owns its summary; no round headers exist at all."""
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const renders=[];
      const surface=createConversationSurface(root,{renderBlock(node,b){
        renders.push(b.blockId);node.textContent=b.blockId;
      }});
      const own=(id,n,toolName,extra={})=>block(id,n,'a','tool',{
        round:{toolName,status:'done',...extra}});
      const blocks=[
        block('intent',0,'a','text'),
        own('checklist',0,'todo_write',{results:[{todos:[{content:'Work',status:'pending'}]}]}),
        own('terminal',1,'run_command',{results:[{exitCode:0}]}),
        own('browser',2,'browser_execute_js'),
        own('python',3,'code_exec'),
        own('legacy',4,'todo_write',{query:'Legacy checklist without accepted structured result'}),
        own('mixed-terminal',5,'run_command',{results:[{exitCode:2}]}),
        own('mixed-read',5,'read_files'),
        block('answer',5,'a','text',{deliverable:true}),
      ];
      const before=JSON.stringify(blocks);
      surface.render(vm(turn(blocks)));
      const cards=[...root.querySelectorAll('[data-tool-summary="self-contained"]')];
      const counts={noHeaders:root.querySelectorAll('.conversation-round-header').length===0,
        cards:cards.length,allVisible:cards.every(n=>!n.hidden),
        immutable:before===JSON.stringify(blocks),
        visibleProse:!root.querySelector('[data-block-id="intent"]').hidden,
        toolbarHidden:root.querySelector('.conversation-round-toolbar').hidden,
        mounted:cards.every(n=>renders.includes(n.dataset.blockId)),
        actionStates:[...root.querySelectorAll('[data-action-state]')].map(n=>[n.dataset.blockId,n.dataset.actionState])};
      const first=cards[0];
      for(let i=0;i<8;i++)surface.render(vm(turn(JSON.parse(JSON.stringify(blocks)))));
      counts.retained=first===root.querySelector('[data-block-id="checklist"]');
      counts.authoredOrder=[...root.querySelectorAll('.conversation-block')].map(n=>n.dataset.blockId);
      surface.dispose();return JSON.stringify(counts);
    ''')
    assert result == {
        'noHeaders': True, 'cards': 7, 'allVisible': True,
        'immutable': True, 'visibleProse': True,
        'toolbarHidden': False, 'mounted': True,
        'actionStates': [
            ['checklist', 'returned'], ['terminal', 'returned'],
            ['browser', 'returned'], ['python', 'returned'],
            ['legacy', 'returned'], ['mixed-terminal', 'attention'],
            ['mixed-read', 'returned'],
        ],
        'retained': True,
        'authoredOrder': ['intent', 'checklist', 'terminal', 'browser', 'python',
                         'legacy', 'mixed-terminal', 'mixed-read', 'answer'],
    }


def test_outline_does_not_open_nested_swarm_task_or_history_disclosures(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const surface=createConversationSurface(root,{renderBlock(node){
        if(node.querySelector('.sw-panel')) return;
        node.innerHTML='<section class="sw-panel"><details class="sw-a-task"><summary>Task</summary>Context</details><details class="sw-a-tool-history"><summary>History</summary>Tools</details></section>';
      }});
      const blocks=Array.from({length:4},(_,i)=>block('swarm-'+i,i,'a','tool',{
        round:{toolName:'spawn_agents',status:'running',_swarm:true}}));
      surface.render(vm(turn(blocks,'running')));
      const task=root.querySelector('.sw-a-task');
      const history=root.querySelector('.sw-a-tool-history');
      const initiallyClosed=!task.open&&!history.open;
      root.querySelector('[data-round-action="expandAll"]').click();
      const bulkPreserves=!task.open&&!history.open;
      task.open=true;
      surface.render(vm(turn(JSON.parse(JSON.stringify(blocks)),'running')));
      const readerRetained=task===root.querySelector('.sw-a-task')&&task.open;
      blocks.forEach(b=>b.round.status='done');
      surface.render(vm(turn(blocks,'completed')));
      root.querySelector('[data-round-action="collapseCompleted"]').click();
      const collapsePreserves=task.open&&!history.open;
      surface.dispose();
      return JSON.stringify({initiallyClosed,bulkPreserves,readerRetained,collapsePreserves});
    ''')
    assert result == {
        'initiallyClosed': True, 'bulkPreserves': True,
        'readerRetained': True, 'collapsePreserves': True,
    }


def test_swarm_internal_disclosures_are_not_generic_action_evidence(round_bundle):
    result = run_js(round_bundle, HELPERS + r'''
      const root=document.createElement('main');document.body.append(root);
      const surface=createConversationSurface(root,{renderBlock(node,b){
        if(b.blockId==='swarm'){
          node.innerHTML='<section class="sw-panel"><details class="sw-a-task"><summary>Task</summary>'
            +'<div>Instructions</div></details><details class="sw-a-history"><summary>History</summary>'
            +'<div>Tool activity</div></details></section>';
        }else node.innerHTML='<details class="generic-evidence"><summary>Tool result</summary><div>Evidence</div></details>';
      }});
      const actions=[block('swarm',0,'a','tool',{round:{toolName:'spawn_agents',status:'running'}}),
        ...Array.from({length:3},(_,i)=>block('generic-'+i,i+1,'a','tool',{
          round:{toolName:'read_files',status:'running'}}))];
      surface.render(vm(turn(actions,'running')));
      const task=root.querySelector('.sw-a-task'),history=root.querySelector('.sw-a-history');
      const initial={task:task.open,history:history.open,
        generic:root.querySelector('.generic-evidence').open};
      root.querySelector('[data-round-action="expandAll"]').click();
      const expanded={task:task.open,history:history.open,
        generic:[...root.querySelectorAll('.generic-evidence')].every(n=>n.open)};
      // A user opens instructions; unrelated completion and bulk collapsing
      // must not undo that intent or open internal tool history.
      task.open=true;actions.forEach(b=>b.round.status='done');
      surface.render(vm(turn(actions)));
      root.querySelector('[data-round-action="collapseCompleted"]').click();
      const collapsed={task:task.open,history:history.open,
        generic:[...root.querySelectorAll('.generic-evidence')].some(n=>n.open)};
      const retained=task===root.querySelector('.sw-a-task');
      surface.dispose();return JSON.stringify({initial,expanded,collapsed,retained});
    ''')
    assert result == {
        'initial': {'task': False, 'history': False, 'generic': True},
        'expanded': {'task': False, 'history': False, 'generic': True},
        'collapsed': {'task': True, 'history': False, 'generic': False},
        'retained': True,
    }
