"""Initial library hydration preserves file-picker DOM and newer selection."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_initial_entry_preserves_upload_control_and_new_source():
    entry = """
import * as library from './library';
import * as session from './session';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { library, session });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperPdfViewer"></div><div class="paper-body"></div>',{url:'http://localhost'}),w=dom.window;
global.window=w;global.document=w.document;global.localStorage=w.localStorage;
global.AbortController=w.AbortController;global.runtimeScope=w;
global.Icon=()=>'';global.t=k=>k;global.escapeHtml=s=>String(s||'');global._paperDescribeDraft='';
w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});w.requestAnimationFrame=f=>setTimeout(f,0);
w._paperLibrary=[];w._paperFolders=[];w._activePaperId='';w._paperHash='';
localStorage.setItem('paper_library_migrated_v1','1');let resolveList;
w.Api={paper:{libraryList:()=>new Promise(r=>resolveList=r),folderList:async()=>({ok:true,folders:[]})}};
(new Function(fs.readFileSync(process.argv[2],'utf8')))();
const source=fs.readFileSync(process.argv[3],'utf8'),start=source.indexOf('function _showPaperLanding()'),end=source.indexOf('\nfunction ',start+1);
w._showPaperLanding=(new Function(source.slice(start,end)+'\nreturn _showPaperLanding;'))();
const check=(v,m)=>{if(!v)throw Error(m)},tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
 let opening=w.session.enterPaperMode();await tick();
 const input=document.querySelector('.paper-upload-btn input');check(input,'initial entry renders upload control');
 resolveList({ok:true,papers:[]});await opening;
 check(input===document.querySelector('.paper-upload-btn input'),'hydration must not replace a file picker target');
 opening=w.session.enterPaperMode();await tick();
 const newPaper=w.library.createPaperEntry('New source');w._paperHash='new-source-hash';w._paperParsedText='NEW SOURCE';
 resolveList({ok:true,papers:[{id:'old',title:'Old source'}]});await opening;
 check(w._activePaperId===newPaper.id&&w._paperHash==='new-source-hash'&&w._paperParsedText==='NEW SOURCE','late entry cannot replace new upload identity');
 check(w._paperLibrary.some(p=>p.id===newPaper.id)&&w._paperLibrary.some(p=>p.id==='old'),'local upload and durable remote rows both survive');
 // A library detail load that began for the prior selection cannot clear a newer one.
 let first=w.library.loadPaperLibrary();await tick();const selected=w.library.createPaperEntry('Newest');
 resolveList({ok:false});await first;check(w._activePaperId===selected.id,'failed older lookup cannot clear latest selection');
 document.documentElement.lang='en';w._showPaperLanding();const english=document.querySelector('.paper-upload-btn input');
 document.documentElement.lang='zh-CN';w._showPaperLanding();check(english!==document.querySelector('.paper-upload-btn input'),'locale change still refreshes landing copy');
 console.log('entry ownership passed');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/session.ts', contents=entry) as bundle:
        result = subprocess.run(['node', '-e', harness, str(ROOT), bundle,
                                 str(ROOT / 'frontend/src/runtime/sections/paper-reader.js')],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
