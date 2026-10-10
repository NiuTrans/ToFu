"""Artifact reading uses measured timing and the current paper's existing owners."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_playback_chapters_source_identity_and_teardown():
    source = ROOT / 'frontend/src/features/paper/podcast-artifact.ts'
    entry = """
import * as owner from './podcast-artifact';
import { setActivePaperId } from './library';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { artifact: owner, setActivePaperId });
"""
    script = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperPodcastContent"></div>',{url:'http://localhost'});
global.window=dom.window;global.document=dom.window.document;global.localStorage=dom.window.localStorage;
global.AbortController=dom.window.AbortController;
const w=window;w._paperHash='hash-a';w._activePaperId='a';
let plays=0,pauses=0,loads=0,asked=[],regens=0,exports=0,videoCleanup=0;
w.HTMLMediaElement.prototype.play=function(){plays++;return Promise.resolve()};
w.HTMLMediaElement.prototype.pause=function(){pauses++};
w.HTMLMediaElement.prototype.load=function(){loads++};
w._paperAskQuestion=q=>asked.push(q);w._podcastGenerate=()=>regens++;w._podcastExportScript=()=>exports++;
w._registeredModels=[];w.isChatModel=()=>true;
eval(fs.readFileSync(process.argv[2],'utf8'));
const owner=w.artifact,host=document.querySelector('#paperPodcastContent');
const assert=(c,m)=>{if(!c)throw Error(m)};
const state={paperHash:'hash-a',status:'done',mode:'short',lang:'en',artifactModel:'model-a',data:{
 audioUrl:'/api/paper/audio/a.wav',durationSec:30,
 script:{title:'<Source overview>',segments:[{section:'problem',text:'The source question.'},{section:'method',text:'The measured method.'}]},
 meta:{source_kind:'report_en',container:'wav',timelineV1:{format:'paper.podcast.timeline.v1',basis:'measured_pcm',durationSec:30,
 segments:[{index:0,startSec:0,endSec:12},{index:1,startSec:12.8,endSec:30}]}}
}};
w._podcast=state;
owner.renderPodcastArtifact(host,state);const audio=document.querySelector('audio');
assert(host.querySelector('h1').textContent==='<Source overview>','title is text');
assert(!host.querySelector('source'),'no title injection');
assert(host.querySelectorAll('.paper-studio-chapter-play').length===2,'measured chapter controls');
host.querySelectorAll('.paper-studio-chapter-play')[1].click();
assert(plays===1,'chapter action starts playback');
assert(audio.currentTime===12.8,'seek to actual PCM boundary, not model estimate');
audio.dispatchEvent(new w.Event('timeupdate'));
assert(host.querySelectorAll('.paper-studio-segment')[1].classList.contains('is-active'),'matching transcript segment highlighted');
assert(host.querySelectorAll('.paper-studio-chapter-play')[1].getAttribute('aria-current')==='true','active chapter accessible');
host.querySelector('.paper-studio-chapter').querySelectorAll('button')[1].click();
assert(asked.length===1,'question delegates once to shared QA');
owner.renderPodcastArtifact(host,state);
assert(document.querySelector('audio')===audio&&audio.currentTime===12.8,'unchanged artifact preserves playback');
const oldButton=host.querySelector('.paper-studio-chapter').querySelectorAll('button')[1];
state.paperHash='hash-b';w._paperHash='hash-b';w._activePaperId='b';oldButton.click();
assert(asked.length===1,'mutated shared state cannot authorize old artifact actions');
owner.destroyPodcastArtifact();assert(pauses===1&&loads===1&&!audio.getAttribute('src')&&!host.children.length,'teardown releases media and DOM');
state.paperHash='hash-b';delete state.data.meta.timelineV1;owner.renderPodcastArtifact(host,state);
assert(!host.querySelector('.paper-studio-chapter-play'),'legacy estimates never authorize seek');
assert(!owner.podcastTimings({...state.data,meta:{timelineV1:{format:'paper.podcast.timeline.v1',basis:'measured_pcm',durationSec:30,
 segments:[{index:0,startSec:0,endSec:20},{index:1,startSec:10,endSec:30}]}}}),'overlapping receipt rejected');
w._destroyPodcastRuntime=()=>owner.destroyPodcastArtifact();w._destroyVideoRuntime=()=>videoCleanup++;
w.setActivePaperId('b');assert(videoCleanup===0,'same selected paper preserves media');
w.setActivePaperId('c');assert(videoCleanup===1&&!host.children.length,'paper switch stops both media owners');
state.paperHash='hash-c';w._paperHash='hash-c';state.status='script_only';owner.renderPodcastArtifact(host,state);
assert(!host.querySelector('audio')&&!host.querySelector('.paper-studio-chapter-play'),'script-only is readable without false playback');
owner.destroyPodcastArtifact();console.log('ok');
"""
    with compiled_typescript(source, contents=entry) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built],
                                text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'ok'
