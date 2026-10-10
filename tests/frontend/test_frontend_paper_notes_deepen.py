"""Notes and deepen keep document/source ownership across late replies."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_notes_and_deepen_source_ownership_and_failed_mutations():
    entry = """
import * as notes from './notes';
import * as deepen from './deepen';
import * as xp from './reading-xp';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { notes, deepen, xp });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div class="paper-report-content" id="paperReportContent"><article class="paper-report-article"><h2>Report</h2><p>Report quote</p></article></div><div class="paper-report-content" id="paperReviewContent"><article class="paper-report-article"><h2>Review</h2><p>Review quote</p></article></div>',{url:'http://localhost'}),w=dom.window;
global.window=w;global.document=w.document;global.localStorage=w.localStorage;
global.AbortController=w.AbortController;
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
w._activePaperId='a';w._paperHash='hash-a';w._paperActiveTab='review';w.t=k=>k;
const views={report:{kind:'report',containerId:'paperReportContent',langKey:()=> 'en'},review:{kind:'review',containerId:'paperReviewContent',langKey:()=> 'review:generic:en'}};
w._reportView=k=>views[k];w.showToast=m=>calls.warnings.push(m);let resolveList,resolveSave,resolveStart,resolvePoll;
const calls={saves:[],starts:[],aborts:[],warnings:[]};let list=()=>new Promise(r=>resolveList=r),save=async()=>({ok:false,error:'offline'}),start=()=>new Promise(r=>resolveStart=r);
w.Api={paper:{notesList:()=>list(),notesCreate:b=>{calls.saves.push(b);return save()},notesUpdate:async()=>({ok:false,error:'offline'}),notesDelete:async()=>null,
 deepenStart:b=>{calls.starts.push(b);return start()},deepenPoll:()=>new Promise(r=>resolvePoll=r),deepenAbort:async id=>{calls.aborts.push(id);return {ok:true}}}};
(new Function(fs.readFileSync(process.argv[2],'utf8')))();
const {notes,deepen}=w,report=document.querySelector('#paperReportContent article'),review=document.querySelector('#paperReviewContent article');
const check=(v,m)=>{if(!v)throw Error(m)},tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
 notes.attachPaperNotes();notes.notesAfterRender(review,null,views.review);
 w._paperHash='hash-b';resolveList({ok:true,notes:[{id:'old',note:'OLD'}]});await tick();
 check(!views.review._paperNotes,'stale list cannot mutate reused view');
 notes.openPaperNoteEditor({quote:'Review quote'},null,0,0);
 document.querySelector('textarea').value='My note';document.querySelector('.paper-note-save').click();await tick();
 check(document.querySelector('textarea').value==='My note'&&document.querySelector('[role=alert]').textContent==='offline','failure preserves editor and reports error');
 check(calls.saves[0].lang==='review:generic:en'&&calls.saves[0].paper_hash==='hash-b','review note uses canonical document');
 save=()=>new Promise(r=>resolveSave=r);document.querySelector('.paper-note-save').click();document.querySelector('.paper-note-save').click();
 check(calls.saves.length===2,'pending note mutation is deduplicated');
 notes.closePaperNoteEditor();w._paperHash='hash-c';notes.openPaperNoteEditor({},null,0,0);
 resolveSave({ok:true,note:{id:'late',note:'OLD'}});await tick();
 check(!views.review._paperNotes&&document.querySelector('dialog'),'late save cannot close newer editor or mutate new source');
 notes.closePaperNoteEditor();list=async()=>({ok:true,notes:[]});
 notes.notesAfterRender(review,null,views.review);await tick();
 check(Array.isArray(views.review._paperNotes),'fresh source list works');
 deepen.paperDeepenAfterRender(review,null,views.review);review.querySelector('.paper-deepen-btn').click();
 check(calls.starts[0].lang==='review:generic:en','review deepen uses clicked document');
 deepen.cancelPaperDeepenJobs();resolveStart({ok:true,task_id:'late-start'});await tick();
 check(calls.aborts.includes('late-start')&&!review.querySelector('.paper-deepen-drawer'),'late deepen start is aborted');
 start=async()=>({ok:true,task_id:'pending-poll'});review.querySelector('.paper-deepen-btn').click();await tick();
 w.Api.paper.deepenAbort=async()=>({ok:false});
 deepen.cancelPaperDeepenJobs();await tick();check(calls.warnings.length===1,'unconfirmed cancellation is visible');resolvePoll({ok:true,json:async()=>({ok:true,status:'done',events:[{type:'done',content:'LATE CONTENT'}]})});await tick();
 check(!review.textContent.includes('LATE CONTENT'),'late poll fenced after Stop');
 deepen.paperDeepenAfterRender(report,null,views.report);start=async()=>({ok:true,cached:true,content:'RIGHT REPORT'});
 report.querySelector('.paper-deepen-btn').click();await tick();
 check(calls.starts.at(-1).lang==='en'&&report.textContent.includes('RIGHT REPORT'),'report deepen remains separate');
 w._paperHash='note-list-race';list=()=>new Promise(r=>resolveList=r);
 notes.notesAfterRender(review,null,views.review);const oldList=resolveList;
 save=async()=>({ok:true,note:{id:'new-note',note:'Fresh note'}});
 notes.openPaperNoteEditor({},null,0,0);document.querySelector('textarea').value='Fresh note';document.querySelector('.paper-note-save').click();await tick();
 oldList({ok:true,notes:[]});await tick();check(w.xp.paperXpGet(views.review,'_paperNotes').some(n=>n.id==='new-note'),'older list cannot erase successful note');
 deepen.destroyPaperDeepen();w.__tofuPaperNotesDestroy();
 for(let i=0;i<100;i++){w.xp.paperXpSet(views.report,'_xpInsight',{thesis:'Source '+i});w._paperHash='replacement-'+i;check(w.xp.paperXpGet(views.report,'_xpInsight')===undefined,'same-id source replacement clears XP projection');}
 w.xp.destroyPaperXP();console.log('notes/deepen passed');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/notes.ts', contents=entry) as bundle:
        result = subprocess.run(['node', '-e', harness, str(ROOT), bundle], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
