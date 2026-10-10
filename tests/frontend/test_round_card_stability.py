"""Rich-leaf reconciliation contracts, bundled with the production Vite pipeline.

Only presenter HTML is reconciled: the Surface still owns leaf lifetimes and
intent delegation. The browser companion exercises the same helper with layout.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLER = ROOT / "scripts/vite_test_bundle.mjs"
pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def round_card_stability_bundle(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node required")
    directory = tmp_path_factory.mktemp("round_card_stability")
    entry = directory / "entry.ts"
    entry.write_text("\n".join(
        f"export * from {json.dumps(str(ROOT / path))};" for path in (
            "frontend/src/conversation/index.ts",
            "frontend/src/conversation/ui/rich-card-reconciliation.ts",
            "frontend/src/conversation/presentation/tool-command-execution-presentation.ts",
            "frontend/src/conversation/presentation/tool-human-guidance-presentation.ts",
            "frontend/src/conversation/presentation/turn-provenance.ts",
        )
    ))
    built = directory / "round_card_stability.js"
    result = subprocess.run(
        ["node", str(BUNDLER), str(entry), "--bundle", "--format=iife",
         "--platform=browser", "--global-name=roundCardStability",
         f"--outfile={built}"], cwd=ROOT, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return built


def _run(bundle, source):
    result = subprocess.run(["node", "-e", r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><main id="root"></main>', {
  pretendToBeVisual:true, runScripts:'outside-only', url:'http://localhost/',
});
const { document } = dom.window;
global.window = dom.window;
global.document = document;
global.HTMLElement = dom.window.HTMLElement;
global.HTMLDetailsElement = dom.window.HTMLDetailsElement;
dom.window.eval(fs.readFileSync(process.argv[1], 'utf8'));
const feature = dom.window.roundCardStability;
const patch = feature.reconcileRichCardHtml;
const root = document.getElementById('root');
""" + source + "\ndom.window.close(); console.log('round_card_stability: PASS');",
        str(bundle)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "round_card_stability: PASS" in result.stdout


@pytest.mark.parametrize("kind", ["tool", "program", "injections", "provenance", "footer"])
def test_round_card_stability_all_rich_leaf_ports(round_card_stability_bundle, kind):
    _run(round_card_stability_bundle, "const kind=" + json.dumps(kind) + r""";
let revision = 0;
let rich = true;
const html = () => rich ? `<details class="rich-${kind}"><summary tabindex="0">${kind}</summary>
  <p class="stable">unchanged selectable content</p><input name="draft" value="server">
  <pre class="scroll">${revision}</pre><button data-conversation-action="approve">Approve</button></details>` : '';
const renderers = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:String, renderToolBlockHtml:html,
  renderInjectionBlockHtml:html, renderProvenanceBlockHtml:html, renderTurnFooterHtml:html,
});
const turn = {turnId:'turn',actor:'assistant',status:kind==='footer'?'completed':'running',segments:[],source:{projection:{}}};
const context = {conversationId:'fixture',turn,lane:{laneId:'main'}};
const block = {kind,id:'block',name:'fixture',channel:'inbox',items:[],value:{},round:{},result:{}};
const paint = () => kind === 'footer' ? renderers.renderTurnFooter(root,turn,context)
  : renderers.renderBlock(root,block,context);
paint();
const details = root.querySelector('details');
const summary = details.querySelector('summary');
const text = details.querySelector('.stable').firstChild;
const input = details.querySelector('input');
const scroll = details.querySelector('pre');
const button = details.querySelector('button');
details.open = true;
summary.focus();
scroll.scrollTop = 29; scroll.scrollLeft = 11;
input.value = 'reader draft';
const selection = document.getSelection();
selection.setBaseAndExtent(text,3,text,12);
let clicks=0;
button.addEventListener('click',()=>clicks++);
let bindings=0;
const add = dom.window.EventTarget.prototype.addEventListener;
dom.window.EventTarget.prototype.addEventListener = function(...args) {
  if (this === root || root.contains(this)) bindings++;
  return add.apply(this,args);
};
for (revision=1;revision<=30;revision++) paint();
assert.equal(root.querySelector('details'),details);
assert.equal(details.querySelector('summary'),summary);
assert.equal(document.activeElement,summary);
assert.equal(details.open,true);
assert.equal(details.querySelector('.stable').firstChild,text);
assert.equal(selection.anchorNode,text);
assert.equal(selection.anchorOffset,3);
assert.equal(selection.focusOffset,12);
assert.equal(scroll.scrollTop,29); assert.equal(scroll.scrollLeft,11);
assert.equal(scroll.textContent,'30');
assert.equal(input.value,'reader draft');
assert.equal(details.querySelector('button'),button);
button.click(); assert.equal(clicks,1); assert.equal(bindings,0);
input.focus(); input.setSelectionRange(2,8,'backward'); revision++; paint();
assert.equal(document.activeElement,input);
assert.equal(input.selectionStart,2); assert.equal(input.selectionEnd,8);
assert.equal(input.selectionDirection,'backward');
dom.window.EventTarget.prototype.addEventListener = add;
// Leaving rich mode invalidates the HTML cache; returning must mount afresh.
rich=false; paint(); assert.equal(details.isConnected,false);
rich=true; paint(); assert.notEqual(root.querySelector('details'),details);
assert.equal(root.querySelector('details').open,false);
""")


def test_round_card_stability_keys_reorders_insertions_duplicates_and_removal(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const card = (key,label=key,open='') => `<details data-rich-key="${key}" ${open}><summary tabindex="0">${label}</summary><p>${key} text</p></details>`;
patch(root,card('a')+card('b'));
const a=root.children[0], b=root.children[1], summary=b.firstChild, text=b.querySelector('p').firstChild;
b.open=true; summary.focus(); b.scrollTop=21;
const selection=document.getSelection(); selection.setBaseAndExtent(text,1,text,5);
patch(root,card('x')+card('b','B changed')+card('a'));
assert.equal(root.children[1],b); assert.equal(root.children[2],a);
assert.equal(b.open,true); assert.equal(a.open,false);
assert.equal(document.activeElement,summary); assert.equal(b.scrollTop,21);
assert.equal(selection.anchorNode,text); assert.equal(selection.toString(),' tex');
patch(root,card('b')+card('a'));
assert.equal(root.children[0],b); assert.equal(root.children[1],a);
patch(root,card('a')); assert.equal(b.isConnected,false);
patch(root,card('a')+card('b')); assert.notEqual(root.children[1],b);
// Duplicate explicit identities use queues, not a last-wins Map.
patch(root,card('d','first')+card('d','second'));
const [first,second]=root.children; first.open=true;
patch(root,card('d','first updated')+card('d','second updated')+card('d','new'));
assert.equal(root.children[0],first); assert.equal(root.children[1],second);
assert.equal(first.open,true); assert.equal(second.open,false);
// Real batch rows have no key: description/path, not numeric ordinal/status,
// identify repeated sibling disclosures when new rows are prepended/reordered.
const edit=(path,index,status)=>`<details class="ptool-batch-done-edit ${status}"><summary><b>${index}</b><span class="ptool-batch-path">${path}</span><span class="ptool-batch-desc">edit</span></summary><pre>${status}</pre></details>`;
patch(root,edit('a.ts',1,'pending')+edit('b.ts',2,'pending'));
const ea=root.children[0],eb=root.children[1]; eb.open=true;
patch(root,edit('new.ts',1,'done')+edit('b.ts',2,'done')+edit('a.ts',3,'done'));
assert.equal(root.children[1],eb); assert.equal(root.children[2],ea);
assert.equal(eb.open,true); assert.equal(root.children[0].open,false);
// Changing a delegated operation must not silently retarget a focused node.
patch(root,'<button data-tofu-action="approve(this)">A</button>');
const oldButton=root.firstChild; oldButton.focus();
patch(root,'<button data-tofu-action="deny(this)">D</button>');
assert.notEqual(root.firstChild,oldButton); assert.equal(oldButton.isConnected,false);
""")


def test_round_card_stability_reader_classes_swarm_lifecycle_and_attributes(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const swarm=(status,open='')=>`<div class="sw-panel"><div class="sw-agent ${open}" data-agent-id="a" data-sw-agent-status="${status}"><button class="sw-a-header">${status}</button><div class="sw-a-objective" data-rich-key="objective">objective</div></div></div>`;
patch(root,swarm('pending'));
const panel=root.firstChild,agent=panel.firstChild,header=agent.firstChild;
panel.classList.add('sw-collapsed');
patch(root,swarm('running','sw-a-open'));
assert.equal(root.firstChild,panel); assert.equal(panel.firstChild,agent);
assert.equal(panel.classList.contains('sw-collapsed'),true);
assert.equal(agent.classList.contains('sw-a-open'),true);
agent.classList.remove('sw-a-open'); agent.dataset.swReaderOpen='false';
patch(root,swarm('done'));
patch(root,swarm('running','sw-a-open'));
assert.equal(agent.classList.contains('sw-a-open'),false);
assert.equal(agent.dataset.swReaderOpen,'false'); assert.equal(agent.firstChild,header);
const generic=(version)=>`<div class="turn-prov tp-expandable"><div class="ptool-results"><button class="header" aria-expanded="false">${version}</button></div><div class="eb-item"><button aria-expanded="false">Evidence</button></div><div data-collapsible="1" class="ptool-panel collapsed"><button aria-expanded="false">Panel</button></div><div class="timer-watcher-body">${version}</div></div>`;
patch(root,generic(1));
const provenance=root.firstChild,search=root.querySelector('.ptool-results'),evidence=root.querySelector('.eb-item'),genericPanel=root.querySelector('.ptool-panel'),timer=root.querySelector('.timer-watcher-body');
provenance.classList.add('tp-expanded'); search.classList.add('expanded'); evidence.classList.add('eb-expanded'); timer.classList.add('expanded'); genericPanel.classList.remove('collapsed');
for(const button of root.querySelectorAll('button')) button.setAttribute('aria-expanded','true');
patch(root,generic(2));
assert.equal(root.firstChild,provenance); assert.ok(provenance.classList.contains('tp-expanded'));
assert.ok(search.classList.contains('expanded')); assert.ok(evidence.classList.contains('eb-expanded')); assert.ok(timer.classList.contains('expanded'));
assert.ok(!genericPanel.classList.contains('collapsed'));
for(const button of root.querySelectorAll('button')) assert.equal(button.getAttribute('aria-expanded'),'true');
patch(root,'<details><summary>A</summary><button disabled title="old" data-obsolete="1" data-tofu-action="action(this)">Old</button></details>');
const details=root.firstChild,button=details.querySelector('button'); details.open=true;
patch(root,'<details><summary>B</summary><button title="new" data-tofu-action="action(this)">New</button></details>');
assert.equal(root.firstChild,details); assert.equal(details.open,true);
assert.equal(details.querySelector('button'),button); assert.equal(button.disabled,false);
assert.equal(button.hasAttribute('data-obsolete'),false); assert.equal(button.title,'new');
assert.equal(button.textContent,'New');
""")


def test_round_card_stability_real_command_presenter_folds_and_settlement(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const presenter=feature.createToolCommandExecutionPresentation({translate:key=>key});
const header={iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''};
const long='printf '+ 'line\n'.repeat(30);
let round={toolName:'run_command',roundNum:1,toolCallId:'cmd-1',query:long,toolArgs:{description:'Run fixture'},_partialOutput:'live 1'};
const running=(interaction)=>presenter.renderRunningCommandHtml(round,header,interaction);
const settled=(interaction,output='finished')=>presenter.renderSettledCommandHtml(round,{exitCode:0,output},header,interaction);
const closed={bodyExpanded:false,outputExpanded:false}, open={bodyExpanded:true,outputExpanded:true};
patch(root,running(closed));
const card=root.firstChild,cmdFold=card.querySelector('.ptool-cmd-fold-cmd');
assert.ok(card.querySelector('.ptool-cmd-output-live'));
// Simulate the real toggle's DOM effects, then receive an older/default snapshot.
card.classList.add('cmd-open'); cmdFold.setAttribute('aria-expanded','true'); cmdFold.focus();
round={...round,_partialOutput:'live 2'}; patch(root,running(closed));
assert.equal(root.firstChild,card); assert.ok(card.classList.contains('cmd-open'));
assert.equal(card.querySelector('.ptool-cmd-fold-cmd'),cmdFold);
assert.equal(cmdFold.getAttribute('aria-expanded'),'true'); assert.equal(document.activeElement,cmdFold);
// Existing runtime interaction sets survive command settlement. Always-visible
// live output is NOT a reader choice and must not force settled output open.
patch(root,settled({bodyExpanded:true,outputExpanded:false}));
assert.equal(root.firstChild,card); assert.ok(card.classList.contains('cmd-open'));
assert.ok(!card.classList.contains('output-open')); assert.ok(!card.classList.contains('ptool-cmd-running'));
const outFold=card.querySelector('.ptool-cmd-fold-output');
card.classList.add('output-open'); outFold.setAttribute('aria-expanded','true'); outFold.focus();
patch(root,settled(closed,'finished plus receipt'));
assert.ok(card.classList.contains('cmd-open')); assert.ok(card.classList.contains('output-open'));
assert.equal(outFold.getAttribute('aria-expanded'),'true'); assert.equal(document.activeElement,outFold);
// A user close must survive an older open snapshot as well.
card.classList.remove('cmd-open','output-open'); cmdFold.setAttribute('aria-expanded','false'); outFold.setAttribute('aria-expanded','false');
patch(root,settled(open,'updated receipt'));
assert.ok(!card.classList.contains('cmd-open')); assert.ok(!card.classList.contains('output-open'));
assert.equal(cmdFold.getAttribute('aria-expanded'),'false'); assert.equal(outFold.getAttribute('aria-expanded'),'false');
patch(root,settled(open,'')); assert.equal(card.querySelector('.ptool-cmd-fold-output'),null);
assert.ok(!card.classList.contains('output-open'));
// Short commands gain data-output-key only when settled: keep the same card.
root.replaceChildren(); round={...round,query:'pwd',toolCallId:'short'};
patch(root,running(closed)); const short=root.firstChild;
patch(root,settled(closed)); assert.equal(root.firstChild,short);
// Running without prior output gets the presenter's live-open default.
root.replaceChildren(); round={...round,_partialOutput:''};
patch(root,running(closed)); const silent=root.firstChild;
round={...round,_partialOutput:'first live line'}; patch(root,running(closed));
assert.equal(root.firstChild,silent); assert.ok(silent.querySelector('.ptool-cmd-output-live'));
""")


def test_round_card_stability_changed_selection_is_not_restored(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
patch(root,'<p>selected text</p>');
const text=root.firstChild.firstChild,selection=document.getSelection();
selection.setBaseAndExtent(text,1,text,9);
let restores=0;
const set=selection.setBaseAndExtent.bind(selection);
selection.setBaseAndExtent=(...args)=>{restores++;return set(...args);};
patch(root,'<p>short</p>');
assert.equal(root.firstChild.firstChild,text); assert.equal(restores,0);
assert.equal(selection.toString(),'');
// Element endpoints with removed children used to have equal null nodeValue;
// restoring their old offsets would throw or re-select changed content.
patch(root,'<p><span>one</span><span>two</span></p>');
const paragraph=root.firstChild; set(paragraph,0,paragraph,2);
patch(root,'<p><span>new</span></p>'); assert.equal(restores,0);
""")


def test_round_card_stability_surface_intents_and_disposal(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const source={turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',status:'running',ordinal:1,currentAttemptId:null,createdAt:1,updatedAt:1,projectionRevision:1,projection:{toolRounds:[],segments:[{type:'tool_use',blockId:'tool:call',id:'call',name:'read_file',input:{},result:{status:'done',content:'v1'}}]}};
const state={conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn:source},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{}};
let intents=0;
const renderers=feature.createClassicConversationRenderers({renderSafeMarkdownHtml:String,
  renderToolBlockHtml:block=>`<details><summary>Result</summary><button data-conversation-action="inspect-tool">${block.result.content}</button></details>`});
const surface=feature.createConversationSurface(root,{...renderers,onIntent:()=>intents++});
const paint=()=>surface.render(feature.selectConversationViewModel(state));
paint(); const button=root.querySelector('[data-conversation-action="inspect-tool"]');
assert.ok(button); root.querySelector('details').open=true;
for(let i=2;i<32;i++) {
  state.turnsById.turn={...source,projectionRevision:i,updatedAt:i,
    projection:{...source.projection,segments:[{...source.projection.segments[0],result:{status:'done',content:'v'+i}}]}};
  state.conversationRevision++; paint();
}
assert.equal(root.querySelector('[data-conversation-action="inspect-tool"]'),button);
assert.equal(button.textContent,'v31');
button.click(); assert.equal(intents,1);
surface.dispose(); assert.equal(button.isConnected,false);
button.click(); assert.equal(intents,1);
""")


def test_round_card_stability_guidance_answers_drafts_and_pending_guard(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const presenter=feature.createToolHumanGuidancePresentation({translate:(key,vars)=>vars?.count===undefined?key:key+vars.count,renderMarkdown:String});
const questions=[{question:'Choose',response_type:'choice',options:[{label:'Keep'},{label:'Change'}]},{question:'Explain'}];
const round={toolCallId:'guidance-1',toolName:'ask_human',status:'awaiting_human',guidanceId:'g1',guidanceType:'batch',guidanceBatchQuestions:questions};
const header={iconHtml:''};
const html=()=>presenter.renderGuidanceHtml(round,header);
patch(root,html());
const card=root.querySelector('.hg-card'),choice=card.querySelector('.hg-batch-choice');
const textarea=card.querySelector('textarea');
assert.ok(choice); assert.ok(textarea);
choice.classList.add('hg-selected');textarea.value='reader answer';textarea.focus();textarea.setSelectionRange(2,7);
questions[1].question='Explain updated';patch(root,html());
assert.equal(root.querySelector('.hg-card'),card);assert.equal(card.querySelector('.hg-batch-choice'),choice);
assert.ok(choice.classList.contains('hg-selected'));assert.equal(textarea.value,'reader answer');
assert.equal(document.activeElement,textarea);assert.equal(textarea.selectionStart,2);
// submitBatch/submitChoice owns this guard until the request resolves. A
// background snapshot must neither enable controls nor clear pending state.
card.classList.add('hg-submitting');
for(const control of card.querySelectorAll('button,textarea')) control.disabled=true;
questions[1].question='Explain while sending';patch(root,html());
assert.ok(card.classList.contains('hg-submitting'));
for(const control of card.querySelectorAll('button,textarea')) assert.equal(control.disabled,true);
card.classList.remove('hg-submitting');for(const control of card.querySelectorAll('button,textarea')) control.disabled=false;
questions[0].options[0].label='Different answer';patch(root,html());
assert.equal(choice.isConnected,false);assert.ok(!card.querySelector('.hg-batch-choice').classList.contains('hg-selected'));
assert.equal(card.querySelector('textarea'),textarea);assert.equal(textarea.value,'reader answer');
""")


def test_round_card_stability_timer_disclosure_label(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const html=(version)=>`<div class="timer-meta-row"><span class="timer-meta-val">${version}</span><span class="timer-meta-more" data-more="More" data-less="Less">More</span></div>`;
patch(root,html(1));const value=root.querySelector('.timer-meta-val'),toggle=root.querySelector('.timer-meta-more');
value.classList.add('expanded');toggle.textContent='Less';patch(root,html(2));
assert.equal(root.querySelector('.timer-meta-val'),value);assert.ok(value.classList.contains('expanded'));
assert.equal(root.querySelector('.timer-meta-more'),toggle);assert.equal(toggle.textContent,'Less');
value.classList.remove('expanded');toggle.textContent='More';patch(root,html(3));assert.equal(toggle.textContent,'More');
""")


def test_new_rich_cards_do_not_read_document_selection(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const getSelection=document.getSelection.bind(document);let reads=0;
document.getSelection=()=>{reads+=1;return getSelection();};
patch(root,'<details><summary>Tool</summary><input name="draft" value="one"></details>');
assert.equal(reads,0,'new empty cards have no reader state to capture');
const detached=document.createElement('section');detached.innerHTML='<p>before</p>';
patch(detached,'<p>after</p>');assert.equal(reads,0);
const input=root.querySelector('input');input.focus();input.value='reader draft';
input.setSelectionRange(2,7);
patch(root,'<details><summary>Tool</summary><input name="draft" value="two"></details>');
assert.equal(reads,1);assert.equal(document.activeElement,input);
assert.equal(input.value,'reader draft');assert.equal(input.selectionStart,2);
""")
