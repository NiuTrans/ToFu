"""Workbench projects canonical evidence and delegates navigation to existing owners."""
import json
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_workbench_grounding_navigation_identity_and_stale_load():
    source = ROOT / 'frontend/src/features/paper/workbench.ts'
    entry = """
import * as owner from './workbench';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as unknown as Record<string, unknown>)[name],
  (name, value) => { (window as unknown as Record<string, unknown>)[name] = value; });
Object.assign(window, { workbench: owner });
"""
    script = r"""
const fs=require('fs'), {JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperModeContainer"><div class="paper-tabs"></div>'+
  ['qa','report','review','translate','podcast','video'].map(id=>`<section class="paper-tab-panel" data-tab="${id}"></section>`).join('')+'</div>');
global.window=dom.window;global.document=dom.window.document;
const w=window;w.t=k=>k;w.paperMode=true;w._paperActiveTab='overview';
w._activePaperId='a';w._paperHash='hash-a';w._paperFileName='<Source A>';
const view={cache:'report',containerId:'paperReportContent',_xpInsight:{items:{thesis:'Grounded central claim'}}};
w._reportView=()=>view;
const report=document.createElement('div');report.id='paperReportContent';
report.innerHTML='<article class="paper-report-article"><p>Source-backed introduction.</p>'+
 Array.from({length:10},(_,i)=>`<h2>Evidence ${i}</h2><p>Measured result ${i}.</p>`).join('')+'</article>';
document.querySelector('[data-tab="report"]').append(report);
eval(fs.readFileSync(process.argv[2],'utf8'));
const o=w.workbench;const assert=(c,m)=>{if(!c)throw Error(m)};
o.mountPaperWorkbench();o.renderPaperWorkbench();
const overview=document.querySelector('[data-tab="overview"]');
assert(overview.textContent.includes('Grounded central claim'),'canonical thesis');
assert(overview.querySelectorAll('ol li').length===6,'bounded outline');
assert(overview.querySelector('h1').textContent==='<Source A>','source escaped');
assert(!overview.querySelector('h1 source'),'no source markup injection');
let opened='';w._switchPaperTab=id=>{opened=id;w._paperActiveTab=id;o.syncPaperWorkbench(id)};
w._setPaperMobileView=()=>{};
const tab=document.querySelector('[role="tab"][data-paper-tab="overview"]');
tab.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
assert(opened==='qa','keyboard delegates QA');
o.syncPaperWorkbench('overview');
assert(document.querySelector('[data-tab="qa"]').hidden,'one visible panel');
w._podcast={paperHash:'hash-b',status:'done'};w._pvideo={paperHash:'hash-a',status:'generating'};
o.renderPaperWorkbench();
assert(!overview.querySelector('.paper-workbench-tool-podcast').textContent.includes('workbenchReady'),'other paper media excluded');
assert(overview.querySelector('.paper-workbench-tool-video').textContent.includes('workbenchRunning'),'matching media state');
view.stream={status:'running'};o.renderPaperWorkbench();
assert(!overview.textContent.includes('Grounded central claim'),'partial report not presented as settled');
assert(!overview.querySelector('ol'),'partial outline excluded');
let question='';w._paperAskQuestion=q=>{question=q};
overview.querySelector('.paper-workbench-question button').click();
assert(question==='paper.workbenchQuestionProblem','question delegates to QA');
(async()=>{
  view.stream=null;w._paperActiveTab='overview';
  let resolve;const pending=o.openPaperWorkbench(()=>new Promise(r=>resolve=r));
  w._activePaperId='b';w._paperFileName='Source B';o.leavePaperWorkbench();o.renderPaperWorkbench();
  resolve();await pending;
  assert(overview.querySelector('h1').textContent==='Source B','late load cannot repaint old paper');
  w._paperHash='';w._paperParsedText='';w._paperPdfUrl='';view.cache='';o.renderPaperWorkbench();
  assert([...overview.querySelectorAll('button')].every(b=>b.disabled),'no-source actions disabled');
  console.log(JSON.stringify({ok:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(source, contents=entry) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built],
                                text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip())['ok']


def test_read_only_lookup_preserves_regeneration_intent_without_paid_start():
    source = ROOT / 'frontend/src/features/paper/report-runtime.ts'
    entry = """
import * as owner from './report-runtime';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as unknown as Record<string, unknown>)[name],
  (name, value) => { (window as unknown as Record<string, unknown>)[name] = value; });
Object.assign(window, { reportOwner: owner });
"""
    script = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="report"></div>',{url:'http://localhost'});
global.window=dom.window;global.document=dom.window.document;global.localStorage=dom.window.localStorage;
const w=window;w._activePaperId='a';w._paperHash='a'.repeat(32);w._paperParsedText='source';
const view={kind:'report',cache:'',regenIntentKey:'regen',containerId:'report',langKey:()=> 'en',uiLang:()=> 'en'};
w._reportView=()=>view;w.t=k=>k;let starts=0,lookups=0;
w.Api={paper:{reportResolve:async()=>{lookups++;return {ok:true,report:'Existing report',lang:'en'}},
 reportStart:async()=>{starts++;return {ok:false}}}};
w._renderFinalReport=(el,report)=>el.textContent=report;
eval(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 w.reportOwner.setReportRegenIntent(w._paperHash,'en','regen');
 await w.reportOwner.loadOrGenerateReport(view,{readOnly:true});
 if(starts!==0||lookups!==1||view.cache!=='Existing report'||!localStorage.getItem('regen')) throw Error('read-only lookup started or consumed paid intent');
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(source, contents=entry) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built],
                                text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'ok'
