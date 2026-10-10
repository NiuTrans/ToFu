"""Compiled reader controls use actual headings and freeze source identity."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_reader_navigation_explicit_generation_and_source_fences():
    entry = """
import * as owner from './report-reader';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { owner });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="host"></div>',{url:'http://localhost'}),w=dom.window;
global.window=w;global.document=w.document;global.localStorage=w.localStorage;
global.AbortController=w.AbortController;
w._activePaperId='a';w._paperHash='hash-a';w._paperFileName='<unsafe>.pdf';
let starts=0,sources=0;w._generatePaperReport=()=>starts++;w._generatePaperReview=()=>starts+=10;
w._setPaperMobileView=()=>sources++;
(new Function(fs.readFileSync(process.argv[2],'utf8')))();
const owner=w.owner,host=document.getElementById('host'),view={kind:'report'};
const check=(value,message)=>{if(!value)throw Error(message)};
owner.renderReportReady(host,view,'Ready','Explanation','Generate');
check(starts===0,'mount must be read-only');
const old=host.querySelector('button');old.click();check(starts===1,'explicit generation');
w._paperHash='hash-b';old.click();check(starts===1,'same-id source replacement fences old action');
owner.renderReportReady(host,{kind:'review'},'Review','Explanation','Generate');
host.querySelector('button').click();check(starts===11,'review command stays separate');
const article=document.createElement('article');
for(let i=0;i<40;i++){const h=document.createElement('h2');h.id='heading-'+i;h.textContent='Section '+i;article.append(h)}
host.replaceChildren(article);let scrolls=0;host.scrollTo=()=>scrolls++;
owner.mountReportReader(host,article,view);
check(host.querySelector('h1').textContent==='<unsafe>','source filename is text');
check(!host.querySelector('unsafe'),'source filename is escaped');
check(host.querySelectorAll('[role=menuitem]').length===32,'navigation DOM is bounded');
host.querySelector('[aria-haspopup=menu]').click();
host.querySelector('[role=menuitem]').click();
check(document.activeElement===article.querySelector('h2')&&scrolls===1,'navigation focuses real heading');
const sourceButton=host.querySelector('.paper-document-controls > button');
sourceButton.click();check(sources===1,'source command');
w._activePaperId='b';sourceButton.click();check(sources===1,'old source command fenced');
host.querySelector('[role=menuitem]').click();check(scrolls===1,'old navigation fenced');
owner.destroyReportReaders();sourceButton.click();check(sources===1,'teardown fences detached controls');
console.log('reader controls passed');
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/report-reader.ts', contents=entry) as bundle:
        result = subprocess.run(['node', '-e', harness, str(ROOT), bundle], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
