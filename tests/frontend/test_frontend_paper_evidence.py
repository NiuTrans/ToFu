"""Quotes remain unverified until bounded current-source search; teardown fences results."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_quote_verification_results_cancel_and_paper_identity():
    source = ROOT / 'frontend/src/features/paper/evidence.ts'
    seam = """const searchPaperSource = (window as any).sourceSearch;
    const openPaperSourceMatch = (window as any).sourceOpen;"""
    contents = source.read_text().replace(
        "const { searchPaperSource, openPaperSourceMatch } = await import('./pdf-viewer');", seam)
    contents += """
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { decoratePaperEvidence, destroyPaperEvidence });
"""
    script = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="bubble" class="paper-qa-msg-content"></div>',{url:'http://localhost'});
global.window=dom.window;global.document=dom.window.document;
global.localStorage=dom.window.localStorage;
const w=window;w._activePaperId='a';w._paperHash='hash-a';
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
const assert=(c,m)=>{if(!c)throw Error(m)};
const flush=()=>new Promise(r=>setTimeout(r,0));
let resolve,pendingSignal,queries=[],opened=[];
w.sourceSearch=(q,s)=>{queries.push(q);pendingSignal=s;return new Promise(r=>resolve=r)};
w.sourceOpen=(_result,page)=>{opened.push(page);return true};
eval(fs.readFileSync(process.argv[2],'utf8'));
const bubble=document.querySelector('#bubble');
bubble.innerHTML='<div class="paper-qa-msg-content">'+Array.from({length:8},()=>'<blockquote>Exact source wording.</blockquote>').join('')+'</div>';
w.decoratePaperEvidence(bubble);
assert(bubble.querySelectorAll('button').length===6,'bounded quote actions');
(async()=>{
 const button=bubble.querySelector('button');button.click();
 assert(queries[0]==='Exact source wording.','only quote text searched');
 assert(!document.querySelector('.paper-evidence-matches button'),'no invented page');
 resolve({status:'ready',sourceId:'a',generation:1,matches:[{page:7,excerpt:'Actual <source> wording.'}],truncated:false});await flush();
 assert(document.querySelector('.paper-evidence-matches p').textContent==='Actual <source> wording.','actual excerpt safely rendered');
 document.querySelector('.paper-evidence-matches button').click();
 assert(opened[0]===7&&!document.querySelector('dialog'),'verified page delegates navigation');
 button.click();const late=resolve;w.destroyPaperEvidence();
 assert(pendingSignal.aborted&&!document.querySelector('dialog'),'close aborts and removes dialog');
 late({status:'ready',matches:[{page:99,excerpt:'late'}],truncated:false});await flush();
 assert(!document.querySelector('dialog'),'late result cannot reopen');
 button.click();resolve({status:'ready',matches:[],truncated:false});await flush();
 assert(!document.querySelector('.paper-evidence-matches button'),'miss has no page action');
 assert(document.querySelector('[role="status"]').textContent.includes('evidenceNotFound'),'miss stays unverified');
 button.click();resolve({status:'ready',matches:[],truncated:true});await flush();
 assert(document.querySelector('[role="status"]').textContent.includes('evidenceIncomplete'),'bounded search does not claim exhaustive miss');
 button.click();w._activePaperId='b';w._paperHash='hash-b';
 resolve({status:'ready',matches:[{page:88,excerpt:'wrong paper'}],truncated:false});await flush();
 assert(!document.querySelector('dialog'),'paper switch fences result');
 const count=queries.length;button.click();assert(queries.length===count,'old quote cannot query new paper');
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(source, contents=contents) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built],
                                text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'ok'


def test_pdf_quote_search_whitespace_limits_and_cancellation():
    source = ROOT / 'frontend/src/features/paper/pdf-reader-content.ts'
    entry = """
import { searchDocument } from './pdf-reader-content';
import { PAPER_BUILD_LIMITS } from '../../api/paper-build.generated';
Object.assign(window, { searchDocument, PAPER_BUILD_LIMITS });
"""
    script = r"""
global.window=global;const fs=require('fs');eval(fs.readFileSync(process.argv[1],'utf8'));
const assert=(c,m)=>{if(!c)throw Error(m)};
const doc=(pages)=>({numPages:pages.length,getPage:async n=>({getTextContent:async()=>({items:[{str:pages[n-1]}]})})});
(async()=>{
 const match=await searchDocument(doc(['Other content','The method\n  improves the measured outcome.']),'method   improves',()=>false);
 assert(match.matches[0].page===2&&match.matches[0].excerpt.includes('method improves'),'whitespace-normalized real page match');
 const missing=await searchDocument(doc(['Other content']),'invented quote',()=>false);
 assert(!missing.matches.length&&!missing.truncated,'ordinary miss');
 const bounded=await searchDocument(doc(['word '.repeat(PAPER_BUILD_LIMITS.reader_search_matches+1)]),'word',()=>false);
 assert(bounded.matches.length===PAPER_BUILD_LIMITS.reader_search_matches&&bounded.truncated,'shared result bound');
 let cancel=false;const delayed={numPages:1,getPage:async()=>({getTextContent:async()=>{cancel=true;return {items:[{str:'Exact quote'}]}}})};
 const cancelled=await searchDocument(delayed,'Exact quote',()=>cancel);
 assert(!cancelled.matches.length,'cancelled extraction cannot return matches');
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(source, contents=entry) as built:
        result = subprocess.run(['node', '-e', script, built], text=True,
                                capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'ok'


def test_pdf_source_adapter_rejects_reload_teardown_and_cancelled_results():
    source = ROOT / 'frontend/src/features/paper/pdf-viewer.ts'
    contents = source.read_text().replace(
        "import { createPdfReader, type PdfReader, type PdfDocument } from './pdf-reader';",
        """import type { PdfReader, PdfDocument } from './pdf-reader';
const createPdfReader = (container: HTMLElement, options: unknown): PdfReader =>
  (window as any).makeReader(container, options);""")
    contents += """
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { loadPaperPdf, searchPaperSource, openPaperSourceMatch, destroyPaperPdfViewer });
"""
    script = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperPdfViewer"></div>',{url:'http://localhost'});
global.window=dom.window;global.document=dom.window.document;global.localStorage=dom.window.localStorage;
const w=window;const assert=(c,m)=>{if(!c)throw Error(m)};
let active,resolve,opened=[],mobile='';
w._setPaperMobileView=v=>mobile=v;
w.makeReader=container=>(active={container,document:{},state:{pages:4,loading:false},
 load:async()=>true,destroy(){},goToPage:n=>{assert(mobile==='pdf','show source before navigation');opened.push(n)},
 search:()=>new Promise(r=>resolve=r)});
eval(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 assert((await w.searchPaperSource('quote',new AbortController().signal)).status==='unavailable','not-ready source explicit');
 await w.loadPaperPdf('/a.pdf');
 let search=w.searchPaperSource('quote',new AbortController().signal);
 resolve({matches:[{page:3,excerpt:'Exact quote'}],truncated:false});let result=await search;
 assert(w.openPaperSourceMatch(result,3)&&opened[0]===3,'current real match navigates');
 assert(!w.openPaperSourceMatch(result,99),'unreturned page refused');
 await w.loadPaperPdf('/a.pdf');
 assert(!w.openPaperSourceMatch(result,3),'same-URL reload invalidates old match');
 search=w.searchPaperSource('quote',new AbortController().signal);const oldResolve=resolve;
 w.destroyPaperPdfViewer();await w.loadPaperPdf('/b.pdf');
 oldResolve({matches:[{page:3,excerpt:'old source'}],truncated:false});result=await search;
 assert(result.status==='stale'&&!result.matches.length&&!w.openPaperSourceMatch(result,3),'late old source discarded');
 const controller=new AbortController();search=w.searchPaperSource('quote',controller.signal);controller.abort();
 resolve({matches:[{page:3,excerpt:'late cancelled'}],truncated:false});result=await search;
 assert(result.status==='stale'&&!result.matches.length,'caller cancellation fences results');
 w.destroyPaperPdfViewer();console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(source, contents=contents) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built],
                                text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'ok'
