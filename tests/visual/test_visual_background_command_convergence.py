"""Real Surface/command presenter against the local app; no durable test data."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_background_command_card_converges_without_replacing_other_turns(page, tmp_path, width, language):
    page.set_viewport_size({'width': width, 'height': 850})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='completion-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<html data-theme="tofu"><body><main id="surface"></main></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    for name, source in [
        ('state', 'frontend/src/core/turn-state.ts'),
        ('view-model', 'frontend/src/conversation/presentation/conversation-view-model.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('command', 'frontend/src/conversation/presentation/tool-command-execution-presentation.ts'),
    ]:
        frame.add_script_tag(path=native_module_path(f'.native/bg-visual-{name}.js', ROOT / source))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      const translate=(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(m,n)=>vars?.[n]??m);
      document.documentElement.lang=locale['toolCmd.finished']==='Finished'?'en':'zh';
      const presenter=createToolCommandExecutionPresentation({translate});
      window.renders=0;
      window.surface=createConversationSurface(document.getElementById('surface'),{renderBlock(node,block){
        if(block.kind==='tool'){
          renders++;
          node.innerHTML=presenter.renderSettledCommandHtml(block.round,block.round.results[0],{
            iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''
          },{turnId:'origin',instanceKey:'visual-command',bodyExpanded:true,outputExpanded:true});
        } else node.textContent=block.text||'';
      }});
      const round={toolName:'run_command',roundNum:1,status:'done',toolCallId:'call',
        toolArgs:{command:'echo completed',description:'Background verification'},
        results:[{backgrounded:true,exitCode:'background',backgroundCommandId:'bg_visual'}]};
      const turn={turnId:'origin',conversationId:'visual',role:'assistant',laneId:'main',ordinal:1,
        status:'completed',projectionRevision:1,projection:{toolRounds:[round],segments:[
          {type:'tool_use',id:'call',name:'run_command',input:round.toolArgs,_round:round}]}};
      window.state={...createTurnState('visual'),turnsById:{origin:turn,
        other:{...turn,turnId:'other',ordinal:2,projection:{content:'Unrelated turn stays mounted'}}},
        laneOrder:{main:['origin','other']}};
      window.completion={commandId:'bg_visual',status:'completed',command:'echo completed',
        resultPreview:'completed safely <script>bad</script>',resultTruncated:false,exitCode:0};
      window.render=()=>surface.render(selectConversationViewModel(state));
      render();
      window.originalTurn=document.querySelector('[data-turn-id="origin"]');
      window.otherTurn=document.querySelector('[data-turn-id="other"]');
      window.complete=()=>{state={...state,queueItems:[{queueId:'q',position:0,kind:'background_receipt',text:'done',backgroundCommand:completion}]};render();};
      window.ack=()=>{state={...state,queueItems:[],turnsById:{...state.turnsById,
        receipt:{...turn,turnId:'receipt',ordinal:3,projection:{_bgCommandInjects:[
          {blockId:'ack',count:1,previews:[{commandId:'bg_visual',backgroundCommand:completion}]}]}}},
        laneOrder:{main:['origin','other','receipt']}};render();};
    }""", locale)
    assert frame.locator('.ptool-cmd-background').count() == 1
    frame.evaluate('complete()')
    assert frame.locator('.ptool-cmd-ok').count() == 1
    assert frame.locator('.ptool-cmd-background').count() == 0
    assert '<script>bad</script>' in frame.locator('#surface').inner_text()
    assert frame.locator('#surface script').count() == 0
    assert frame.evaluate('originalTurn===document.querySelector("[data-turn-id=origin]")')
    assert frame.evaluate('otherTurn===document.querySelector("[data-turn-id=other]")')
    frame.evaluate('window.settledCard=document.querySelector(".ptool-cmd-ok");window.settledRenders=renders;ack()')
    assert frame.evaluate('settledCard===document.querySelector(".ptool-cmd-ok")')
    assert frame.evaluate('renders===settledRenders'), 'queue to receipt does not redraw the settled card'
    for _ in range(20):
        frame.evaluate('render()')
    assert frame.evaluate('renders===settledRenders'), 'unchanged snapshots have zero command redraws'
    screenshot = tmp_path / f'background-command-{language}-{width}.png'
    page.locator('#completion-fixture').screenshot(path=str(screenshot))
    print(f'Background command screenshot: {screenshot}')
    frame.evaluate('surface.dispose()')
