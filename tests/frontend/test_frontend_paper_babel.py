"""Translation publishes terminal artifacts only into the selected source scope."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_translation_reader_explicit_start_cancel_and_source_fences():
    entry = """
import * as owner from './babel';
import { setLanguage } from '../../i18n';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { owner, setLanguage });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperTranslateContent"></div>',{url:'http://localhost'});
const w=dom.window;global.window=w;global.document=w.document;global.localStorage=w.localStorage;
global.AbortController=w.AbortController;global.CustomEvent=w.CustomEvent;
Object.defineProperty(global,'navigator',{value:w.navigator,configurable:true});
global.fetch=async()=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(process.argv[1]+'/frontend/src/i18n/locales/en.json','utf8'))});
let starts=[],aborts=[],saves=0,questions=[],subscriptions=[],copyText='',exportText='',revokes=0;
w._activePaperId='paper-a';w._paperHash='hash-a';w._paperFileName='<A>.pdf';w._paperParsedText='SOURCE A';
w._babelTargetLang='';w._babelTranslatedPages={};w.renderMarkdown=text=>'<h2>Methods</h2><p>'+text+'</p><h2>Limits</h2><p>Conditional evidence.</p>';
w._saveActivePaperState=()=>saves++;w._paperAskQuestion=q=>questions.push(q);
w.pushSubscribe=(channel,id,handler)=>subscriptions.push({channel,id,handler});
w.pushUnsubscribe=(channel,id,handler)=>{subscriptions=subscriptions.filter(row=>row.id!==id||row.handler!==handler)};
w.navigator.clipboard={writeText:async text=>{copyText=text}};
global.URL.createObjectURL=blob=>{exportText=blob;return 'blob:test'};global.URL.revokeObjectURL=()=>revokes++;
w.HTMLAnchorElement.prototype.click=function(){};
let cacheImpl=async()=>({ok:true,text:''}),startImpl,pollImpl;
w.Api={paper:{
 translateCache:(hash,lang)=>cacheImpl(hash,lang),
 translateStart:body=>{starts.push(body);return startImpl(body)},
 translateAbort:async id=>{aborts.push(id);return {ok:true}},
 translatePoll:(id,cursor)=>pollImpl(id,cursor)
}};
eval(fs.readFileSync(process.argv[2],'utf8'));
const owner=w.owner,assert=(value,message)=>{if(!value)throw Error(message)},tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const response=page=>new Response(JSON.stringify(page),{headers:{'Content-Type':'application/json'}});
(async()=>{
 await w.setLanguage('en');owner.initBabelPdfTab();
 assert(!starts.length,'opening translation never starts paid work');
 assert(document.querySelector('h1').textContent==='<A>','source title stays safe text');
 owner.switchBabelLang('zh');await tick();assert(!starts.length,'language choice remains read-only');
 let releaseCache;cacheImpl=()=>new Promise(resolve=>releaseCache=resolve);
 const staleCache=owner.babelTranslateAllPages('zh');await tick();
 owner.destroyBabelRuntime();w._activePaperId='paper-b';w._paperHash='hash-b';w._paperParsedText='SOURCE B';w._babelTranslatedPages={};
 cacheImpl=async()=>({ok:true,text:''});owner.initBabelPdfTab();releaseCache({ok:true,text:'OLD CACHE'});await staleCache;await tick();
 assert(!w._babelTranslatedPages.zh&&!document.querySelector('#babelPdfBody').textContent.includes('OLD CACHE'),'same-language cache cannot leak across papers');
 assert(!starts.length,'stale cache miss cannot start a task for the new source');
 let releaseStart;startImpl=()=>new Promise(resolve=>releaseStart=resolve);
 const stopped=owner.babelTranslateAllPages('zh');await tick();owner.stopBabelTranslation();
 assert(!w._babelTranslating,'Stop releases the selected view immediately');
 releaseStart({ok:true,task_id:'late-stopped',paper_hash:'hash-b'});await stopped;
 assert(aborts.includes('late-stopped')&&!w._babelTranslatedPages.zh,'late-created job receives Stop and never publishes');
 assert(starts[0].paper_text==='SOURCE B'&&starts[0].paper_hash==='hash-b','start freezes owned source identity and text');
 // A superseded start cannot release or overwrite a newer run.
 let releaseOld,releaseNew;startImpl=()=>new Promise(resolve=>releaseOld=resolve);
 const old=owner.babelTranslateAllPages('zh');await tick();owner.stopBabelTranslation();
 startImpl=()=>new Promise(resolve=>releaseNew=resolve);
 const newer=owner.babelTranslateAllPages('zh');await tick();releaseOld({ok:true,task_id:'old-attempt'});await old;
 assert(w._babelTranslating,'old finally cannot release the newer run');
 pollImpl=async()=>response({ok:true,status:'done',text:'COMPLETE B',events:[]});
 releaseNew({ok:true,task_id:'new-attempt',paper_hash:'hash-b'});await newer;
 assert(w._babelTranslatedPages.zh==='COMPLETE B'&&saves===1,'only completed selected artifact is persisted');
 const body=document.querySelector('#babelPdfBody'),article=body.firstElementChild;
 owner.initBabelPdfTab();assert(body.firstElementChild===article,'returning preserves the rendered artifact');
 assert(document.querySelectorAll('.paper-translation-chapters button').length===2,'navigation uses actual translated headings');
 document.querySelector('.paper-translation-section-actions button').click();
 assert(questions.length===1&&questions[0].includes('COMPLETE B'),'translation question delegates with original-paper verification instructions');
 document.querySelector('footer button').click();await tick();assert(copyText==='COMPLETE B','copy excludes reader controls');
 document.querySelectorAll('footer button')[1].click();assert(await exportText.text()==='COMPLETE B','download contains the translation artifact only');
 const detachedAsk=document.querySelector('.paper-translation-section-actions button');
 owner.destroyBabelRuntime();assert(revokes===1,'teardown revokes pending export');
 w._activePaperId='paper-c';w._paperHash='hash-c';w._paperParsedText='SOURCE C';w._babelTranslatedPages={};
 detachedAsk.click();assert(questions.length===1,'detached source cannot ask questions');
 owner.initBabelPdfTab();await tick();
 // Old same-language poll and push results cannot paint or persist into C.
 let releasePoll;startImpl=async()=>({ok:true,task_id:'old-poll',paper_hash:'hash-c'});
 pollImpl=()=>new Promise(resolve=>releasePoll=resolve);
 const polled=owner.babelTranslateAllPages('zh');await tick();const stalePush=subscriptions[0].handler;
 owner.destroyBabelRuntime();w._activePaperId='paper-d';w._paperHash='hash-d';w._paperParsedText='SOURCE D';w._babelTranslatedPages={};owner.initBabelPdfTab();await tick();
 stalePush({seq:1,type:'done',text:'WRONG SOURCE'});releasePoll(response({ok:true,status:'done',paper_hash:'hash-c',text:'WRONG SOURCE',events:[]}));await polled;
 assert(aborts.includes('old-poll')&&!w._babelTranslatedPages.zh&&w._paperHash==='hash-d'&&saves===1,'late poll/push stays fenced without mutating source hash');
 // Progress is not publishable when a job aborts or has no terminal text.
 startImpl=async()=>({ok:true,task_id:'interrupted',paper_hash:'hash-d'});
 pollImpl=async()=>response({ok:true,status:'aborted',events:[{seq:0,type:'chunk',index:0,total:2,text:'PARTIAL'}]});
 await owner.babelTranslateAllPages('zh');
 assert(!w._babelTranslatedPages.zh&&document.querySelector('#babelPdfStatus').textContent.includes('failed'),'interrupted task shows failure and never saves partial prose');
 pollImpl=async()=>response({ok:true,status:'done',text:'',events:[]});await owner.babelTranslateAllPages('zh');
 assert(!w._babelTranslatedPages.zh&&saves===1,'empty terminal artifact cannot become a completed translation');
 // Shared seq gate adopts push completion once before an in-flight poll returns.
 startImpl=async()=>({ok:true,task_id:'push-done',paper_hash:'hash-d'});
 pollImpl=()=>new Promise(resolve=>releasePoll=resolve);const pushed=owner.babelTranslateAllPages('zh');await tick();
 const subscription=subscriptions[0];assert(subscription.channel==='paper-translate','shared translation push channel');
 subscription.handler({seq:0,type:'done',text:'COMPLETE D'});
 assert(w._babelTranslatedPages.zh==='COMPLETE D'&&subscriptions.length===0,'push completion publishes and detaches immediately');
 releasePoll(response({ok:true,status:'done',text:'COMPLETE D',events:[]}));await pushed;
 assert(saves===2&&!w._babelTranslating,'push and poll settle exactly once');
 owner.destroyBabelRuntime();w._babelTranslatedPages={};owner.initBabelPdfTab();await tick();
 startImpl=async()=>({ok:true,task_id:'unconfirmed-stop',paper_hash:'hash-d'});
 pollImpl=()=>new Promise(resolve=>releasePoll=resolve);
 w.Api.paper.translateAbort=async id=>{aborts.push(id);return {ok:false}};
 const unconfirmed=owner.babelTranslateAllPages('zh');await tick();owner.stopBabelTranslation();await tick();
 assert(document.querySelector('#babelPdfStatus').textContent.includes('not confirmed'),'failed cancellation acknowledgement stays explicit');
 releasePoll(response({ok:true,status:'done',text:'LATE AFTER STOP',events:[]}));await unconfirmed;
 assert(!w._babelTranslatedPages.zh&&saves===2,'failed stop acknowledgement cannot admit a late artifact');
 owner.destroyBabelRuntime();assert(!subscriptions.length&&!document.querySelector('.paper-translation-reader'),'teardown releases subscriptions and reader DOM');
 w._babelTranslatedPages={zh:'OLD SOURCE CACHE'};owner.initBabelPdfTab();await tick();
 w._paperHash='same-id-replacement';owner.initBabelPdfTab();await tick();
 assert(!w._babelTranslatedPages.zh&&!document.querySelector('#babelPdfBody').textContent.includes('OLD SOURCE CACHE'),'same-id source replacement also invalidates the browser cache binding');
 owner.destroyBabelRuntime();
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/babel.ts', contents=entry) as built:
        result = subprocess.run(['node', '-e', harness, str(ROOT), built], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'
