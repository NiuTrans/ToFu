"""Shared native Paper adapter harness with controllable PDF transport faults."""
from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from tests._esm_feature_harness import compile_feature_owner

ROOT = Path(__file__).resolve().parents[2]
SETUP = r"""
const fs=require('fs'),path=require('path');
const {JSDOM}=require(path.join(process.argv[1],'node_modules/jsdom'));
const dom=new JSDOM('<div id="paperPdfViewer"></div><input id="paperZoomLevel"><input id="paperZoomSlider">',
  {url:'http://localhost/'});
global.window=dom.window;global.document=window.document;
for(const key of ['HTMLElement','Element','AbortController','AbortSignal','localStorage'])global[key]=window[key];
global.requestAnimationFrame=callback=>setTimeout(callback,0);global.cancelAnimationFrame=clearTimeout;
window.requestAnimationFrame=requestAnimationFrame;window.cancelAnimationFrame=cancelAnimationFrame;
window.HTMLCanvasElement.prototype.getContext=()=>({});
window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const root=document.getElementById('paperPdfViewer');
Object.defineProperties(root,{clientWidth:{value:620},clientHeight:{value:880}});
const opened=[],destroyed=[],rasterized=[],downloads=[];
let openFault=null,renderFault=null,hold=null,pages=20;
const entry={};
window._getActivePaperEntry=()=>entry;window._persistPaperEntry=()=>{};
window._renderPaperLibrary=()=>{};window._updatePaperTitles=()=>{};
window.apiUrl=url=>url;window.debugLog=()=>{};
window.Api={paper:{pdfArrayBuffer:async(url,options)=>{
  downloads.push({url,signal:options.signal});return new Uint8Array([37,80,68,70]);
}}};
window.pdfjsLib={TextLayer:class{render(){return Promise.resolve()}cancel(){}},getDocument:options=>{
  const name=options.url||'bytes';opened.push(name);
  const page=number=>({
    getViewport:({scale})=>({width:612*scale,height:792*scale}),
    render:()=>{rasterized.push(number);return{
      promise:renderFault?.(name)?Promise.reject(new Error('range render failed')):Promise.resolve(),cancel(){}}},
    getTextContent:async()=>({items:[]}),getAnnotations:async()=>[],cleanup(){},
  });
  const doc={numPages:pages,getPage:async number=>page(number),destroy:async()=>{destroyed.push(name)}};
  const promise=hold?.(name,doc)||(openFault?.(name)?Promise.reject(new Error('range open failed')):Promise.resolve(doc));
  return{promise,destroy:doc.destroy};
}};
(0,eval)(fs.readFileSync(process.argv[2],'utf8'));
const load=url=>window._loadPaperPdf(url);
const stop=()=>window._destroyPaperPdfViewer();
"""


def run_paper_adapter(tmp_path: Path, body: str) -> dict:
    if not shutil.which('node') or not (ROOT / 'node_modules/jsdom').is_dir():
        pytest.skip('Node/jsdom unavailable')
    built = tmp_path / 'paper-adapter.js'
    compiled = compile_feature_owner(ROOT / 'scripts/vite_test_bundle.mjs',
                                     ROOT / 'frontend/src/features/paper/pdf-viewer.ts', built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    script = SETUP + '\n(async()=>{\n' + body + '\n})().catch(error=>{console.error(error);process.exitCode=1});'
    result = subprocess.run(['node', '-e', script, str(ROOT), str(built)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])
