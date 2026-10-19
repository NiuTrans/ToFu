"""Real-browser rich-card state checks; full app/visual gate stays with root."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from tests.frontend.test_round_card_stability import round_card_stability_bundle as round_card_stability_bundle  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.fixture(scope="module")
def round_card_stability_url(round_card_stability_bundle):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
        QuietHandler, directory=str(round_card_stability_bundle.parent),
    ))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("legacy_move", [False, True], ids=["native-move", "insertBefore-fallback"])
def test_round_card_stability_browser_reader_state(browser, round_card_stability_url, legacy_move):
    page = browser.new_page(viewport={"width": 1000, "height": 720})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(round_card_stability_url)
        page.set_content('<!doctype html><main id="root"></main>')
        page.add_script_tag(url=round_card_stability_url + "round_card_stability.js")
        result = page.evaluate(r"""(legacyMove) => {
          const check=(ok,label)=>{if(!ok) throw new Error(label);};
          if(legacyMove) Object.defineProperty(Element.prototype,'moveBefore',{value:undefined,configurable:true});
          const feature=roundCardStability,root=document.getElementById('root');
          const tall='line with a very long output value '.repeat(12)+'\n';
          const card=(key,version)=>`<details data-rich-key="${key}"><summary tabindex="0">${key}</summary><p class="selected">unchanged selection text</p><textarea name="draft">server value</textarea><div class="scroll" style="width:220px;height:85px;overflow:auto"><pre>${tall.repeat(40)}</pre></div><button data-conversation-action="inspect-tool">${version}</button></details>`;
          const patch=(order,version)=>feature.reconcileRichCardHtml(root,order.map(key=>card(key,version)).join(''));
          patch(['a','b'],0);
          const a=root.children[0],b=root.children[1];b.open=true;
          const summary=b.querySelector('summary'),text=b.querySelector('.selected').firstChild;
          const input=b.querySelector('textarea'),scroll=b.querySelector('.scroll'),button=b.querySelector('button');
          input.value='reader typing a draft'; summary.focus(); scroll.scrollTop=147;scroll.scrollLeft=75;
          const selection=document.getSelection();selection.setBaseAndExtent(text,17,text,2);
          let clicks=0;button.addEventListener('click',()=>clicks++);
          let added=0;
          const add=EventTarget.prototype.addEventListener;
          EventTarget.prototype.addEventListener=function(...args){added++;return add.apply(this,args);};
          for(let version=1;version<=25;version++) patch(version%2?['x','b','a']:['a','b'],version);
          EventTarget.prototype.addEventListener=add;
          check(root.querySelector('[data-rich-key="b"]')===b,'keyed card retained');
          check(root.querySelector('[data-rich-key="a"]')===a,'reordered sibling retained');
          check(b.open,'disclosure retained');check(document.activeElement===summary,'summary focus retained');
          check(selection.anchorNode===text && selection.anchorOffset===17 && selection.focusNode===text && selection.focusOffset===2,'backward DOM selection retained');
          check(scroll.scrollTop===147 && scroll.scrollLeft===75,'real layout scroll retained');
          check(added===0,'no listener rebinding');button.click();check(clicks===1,'single click');
          input.focus();input.setSelectionRange(2,12,'backward');patch(['b','a'],26);
          check(b.querySelector('textarea')===input && document.activeElement===input,'draft focus retained');
          check(input.value==='reader typing a draft' && input.selectionStart===2 && input.selectionEnd===12 && input.selectionDirection==='backward','typed draft and range retained');
          patch(['a'],27);check(!b.isConnected,'removed node detached');patch(['a','b'],28);
          check(root.children[1]!==b && !root.children[1].open,'removed state not resurrected');
          return {legacyMove,nativeMoveAvailable:typeof Element.prototype.moveBefore==='function',clicks,added};
        }""", legacy_move)
        assert result["clicks"] == 1
        assert result["added"] == 0
        assert not errors
    finally:
        page.close()


def test_round_card_stability_browser_command_and_surface_teardown(browser, round_card_stability_url):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(round_card_stability_url)
        page.set_content('<!doctype html><main id="root"></main>')
        page.add_script_tag(url=round_card_stability_url + "round_card_stability.js")
        page.evaluate(r"""() => {
          const check=(ok,label)=>{if(!ok) throw new Error(label);};
          const feature=roundCardStability,root=document.getElementById('root');
          const presenter=feature.createToolCommandExecutionPresentation({translate:key=>key});
          const header={iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''};
          const round={toolName:'run_command',roundNum:1,toolCallId:'command',query:'echo line\n'.repeat(30),toolArgs:{description:'Run fixture'},_partialOutput:'running'};
          const closed={bodyExpanded:false,outputExpanded:false};
          feature.reconcileRichCardHtml(root,presenter.renderRunningCommandHtml(round,header,closed));
          const card=root.firstChild,fold=card.querySelector('.ptool-cmd-fold-cmd');
          card.classList.add('cmd-open');fold.setAttribute('aria-expanded','true');fold.focus();
          feature.reconcileRichCardHtml(root,presenter.renderRunningCommandHtml({...round,_partialOutput:'progress'},header,closed));
          check(root.firstChild===card && card.classList.contains('cmd-open'),'command snapshot kept reader fold');
          check(document.activeElement===fold && fold.getAttribute('aria-expanded')==='true','command fold focus/aria');
          feature.reconcileRichCardHtml(root,presenter.renderSettledCommandHtml(round,{exitCode:0,output:'finished'},header,{bodyExpanded:true,outputExpanded:false}));
          check(root.firstChild===card && !card.classList.contains('ptool-cmd-running') && !card.classList.contains('output-open'),'settlement lifecycle');
          root.replaceChildren();
          const source={turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',status:'running',ordinal:1,currentAttemptId:null,createdAt:1,updatedAt:1,projectionRevision:1,projection:{toolRounds:[],segments:[{type:'tool_use',blockId:'tool:call',id:'call',name:'read_file',input:{},result:{status:'done',content:'v1'}}]}};
          const state={conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn:source},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{}};
          let intents=0;
          const renderers=feature.createClassicConversationRenderers({renderSafeMarkdownHtml:String,renderToolBlockHtml:block=>`<details><summary>result</summary><button data-conversation-action="inspect-tool">${block.result.content}</button></details>`});
          const surface=feature.createConversationSurface(root,{...renderers,onIntent:()=>intents++});
          const paint=()=>surface.render(feature.selectConversationViewModel(state));paint();
          const button=root.querySelector('[data-conversation-action="inspect-tool"]');
          root.querySelector('details').open=true;button.focus();
          for(let i=2;i<=20;i++){
            state.turnsById.turn={...source,projectionRevision:i,updatedAt:i,
              projection:{...source.projection,segments:[{...source.projection.segments[0],result:{status:'done',content:'v'+i}}]}};
            state.conversationRevision++;paint();
          }
          check(root.querySelector('[data-conversation-action="inspect-tool"]')===button && document.activeElement===button,'Surface keeps button');
          check(button.textContent==='v20','Surface renders latest immutable result');
          button.click();check(intents===1,'one delegated intent');
          surface.dispose();check(!button.isConnected,'Surface released rich leaf');button.click();check(intents===1,'detached card cannot emit an intent');
          // Re-mount to the same root: dispose must have removed the old root listener.
          const remount=feature.createConversationSurface(root,{...renderers,onIntent:()=>intents++});
          remount.render(feature.selectConversationViewModel(state));root.querySelector('details').open=true;
          root.querySelector('[data-conversation-action="inspect-tool"]').click();check(intents===2,'no duplicate listener on remount');remount.dispose();
        }""")
        assert not errors
    finally:
        page.close()


@pytest.mark.parametrize("legacy_move", [False, True], ids=["native-move", "insertBefore-fallback"])
def test_round_card_stability_browser_surface_reorder(browser, round_card_stability_url, legacy_move):
    page = browser.new_page()
    try:
        page.goto(round_card_stability_url)
        page.set_content('<!doctype html><main id="root"></main>')
        page.add_script_tag(url=round_card_stability_url + "round_card_stability.js")
        verified = page.evaluate(r"""legacyMove => {
          const check=(ok,label)=>{if(!ok) throw new Error(label);};
          if(legacyMove) Object.defineProperty(Element.prototype,'moveBefore',{value:undefined,configurable:true});
          else check(typeof Element.prototype.moveBefore==='function','native move path available');
          const feature=roundCardStability,root=document.getElementById('root');
          const segment=(id,version)=>({type:'tool_use',blockId:'tool:'+id,id,name:'read_file',input:{},result:{status:'done',content:'v'+version}});
          const source={turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',status:'running',ordinal:1,currentAttemptId:null,createdAt:1,updatedAt:1,projectionRevision:1,projection:{toolRounds:[],segments:[segment('a',1),segment('b',1)]}};
          const state={conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn:source},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{}};
          let intents=0;
          const renderers=feature.createClassicConversationRenderers({renderSafeMarkdownHtml:String,
            renderToolBlockHtml:block=>`<details><summary>Result</summary><p class="selected">unchanged selection text</p><button data-conversation-action="inspect-tool">${block.result.content}</button></details>`});
          const surface=feature.createConversationSurface(root,{...renderers,onIntent:()=>intents++});
          const paint=()=>surface.render(feature.selectConversationViewModel(state));paint();
          const wrapper=root.querySelector('[data-block-id="tool:b"]'),card=wrapper.querySelector('details');
          const summary=card.querySelector('summary'),button=card.querySelector('button'),text=card.querySelector('.selected').firstChild;
          card.open=true;summary.focus();
          const selection=document.getSelection();selection.setBaseAndExtent(text,17,text,2);
          for(let version=2;version<=8;version++) {
            state.turnsById.turn={...source,projectionRevision:version,updatedAt:version,
              projection:{...source.projection,segments:(version%2?['a','b']:['b','a']).map(id=>segment(id,version))}};
            state.conversationRevision++;paint();
            check(root.querySelector('[data-block-id="tool:b"]')===wrapper && wrapper.querySelector('details')===card,'outer block and card retained');
            check(card.open && document.activeElement===summary,'whole-card move preserves disclosure and focus');
            check(selection.anchorNode===text && selection.anchorOffset===17 && selection.focusNode===text && selection.focusOffset===2,'whole-card move preserves backward selection');
            check(button.textContent==='v'+version,'immutable update renders latest result');
          }
          state.turnsById.turn=JSON.parse(JSON.stringify(state.turnsById.turn));paint();
          check(card.querySelector('button')===button,'equivalent replay retains action');
          button.click();check(intents===1,'exactly one latest intent');surface.dispose();
          return true;
        }""", legacy_move)
        assert verified is True
    finally:
        page.close()
