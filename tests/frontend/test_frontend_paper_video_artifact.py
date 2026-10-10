"""Program-time scene reading, one film, optional previews and source teardown."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_video_reader_uses_program_clock_and_preserves_playback():
    entry = """
import * as owner from './video-artifact';
import { setLanguage } from '../../i18n';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name,value) => { (window as any)[name]=value; });
Object.assign(window, { owner, setLanguage });
"""
    script = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperVideoContent"></div>',{url:'http://localhost'});
global.window=dom.window;global.document=dom.window.document;global.localStorage=window.localStorage;
global.AbortController=window.AbortController;global.CustomEvent=window.CustomEvent;
global.fetch=async()=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(process.argv[1]+'/frontend/src/i18n/locales/en.json','utf8'))});
const w=window;w._paperHash='hash-a';w._activePaperId='a';w._paperFileName='<Source>.pdf';
let plays=0,pauses=0,loads=0,asked=[],regens=[],sceneLoads=0;
w.HTMLMediaElement.prototype.play=function(){plays++;this._playing=true;return Promise.resolve()};
w.HTMLMediaElement.prototype.pause=function(){pauses++;this._playing=false};
w.HTMLMediaElement.prototype.load=function(){loads++};
Object.defineProperty(w.HTMLMediaElement.prototype,'paused',{get(){return !this._playing}});
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
w._registeredModels=[];w.isChatModel=()=>true;
w._paperAskQuestion=q=>asked.push(q);w._videoRegenScene=id=>regens.push(id);w._pvLoadScenes=()=>sceneLoads++;
w.Api={motion:{fileUrl:(id,kind)=>'/film/'+id+(kind?'/'+kind:''),sceneFileUrl:(id,sid)=>'/scene/'+id+'/'+sid}};
eval(fs.readFileSync(process.argv[2],'utf8'));
(async () => {
await w.setLanguage('en');
const owner=w.owner,host=document.querySelector('#paperVideoContent');
const assert=(c,m)=>{if(!c)throw Error(m)};
const state={paperHash:'hash-a',status:'done',lang:'en',quality:'standard',_doneTaskId:'film-a',result:{duration:30,narrated:true},scenes:[
 {scene_id:'a',text:'The research question.',on_screen:'The claim',has_video:true,start:0,end:5,timeline_contract_version:'motion-timeline-v1',timeline_start_s:0,timeline_end_s:12.8},
 {scene_id:'b',text:'The controlled comparison.',on_screen:'The evidence',has_video:true,start:5,end:10,timeline_contract_version:'motion-timeline-v1',timeline_start_s:12.8,timeline_end_s:30}
]};w._pvideo=state;
owner.renderVideoArtifact(host,state);const film=document.querySelector('#paperVideoPlayer');
Object.defineProperty(film,'duration',{value:30});
assert(host.querySelector('h1').textContent==='<Source>','source name remains text');
assert(host.querySelectorAll('video').length===1,'one permanent film, no eagerly decoded scene grid');
host.querySelectorAll('.paper-studio-chapter-play')[1].click();
assert(film.currentTime===12.8&&plays===1,'chapter starts at actual program clock, not planned start');
film.dispatchEvent(new w.Event('timeupdate'));
assert(host.querySelectorAll('.paper-studio-segment')[1].classList.contains('is-active'),'narration tracks the film');
host.querySelectorAll('.paper-studio-chapter')[1].querySelectorAll('button')[1].click();
assert(asked.length===1,'chapter delegates to existing QA once');
owner.renderVideoArtifact(host,state);assert(document.querySelector('#paperVideoPlayer')===film&&film.currentTime===12.8,'identical render preserves playback');
film.play=()=>Promise.reject(Object.assign(new Error('paused before play settled'),{name:'AbortError'}));
host.querySelectorAll('.paper-studio-chapter-play')[1].click();
await new Promise(resolve=>setTimeout(resolve,0));
assert(!host.querySelector('.paper-studio-feedback').textContent,'intentional play interruption is not a media failure');
delete film.play;
film.playbackRate=1.25;film.dispatchEvent(new w.Event('ratechange'));
assert(host.querySelector('.ui-dropdown button').textContent.includes('1.25'),'speed label follows restored/native playback rate');
host.querySelector('.paper-studio-chapter').querySelectorAll('button')[2].click();
assert(document.querySelectorAll('video').length===2&&film.paused,'preview is on demand and pauses the film');
document.querySelector('dialog').querySelectorAll('button')[1].click();
assert(regens[0]==='a'&&!document.querySelector('dialog'),'scene update delegates to authoritative owner');
state.scenesLoading=true;owner.renderVideoArtifact(host,state);
assert(document.querySelector('#paperVideoPlayer')===film,'loading chapters does not recreate film');
state.scenesLoading=false;state.scenesError='unavailable';owner.renderVideoArtifact(host,state);
host.querySelector('.paper-studio-chapters > button').click();assert(sceneLoads===1,'chapter failure has a retry');
owner.renderVideoArtifact(host,state,123);film.dispatchEvent(new w.Event('loadedmetadata'));
assert(film.src.includes('v=123')&&film.currentTime===12.8,'applied revision refreshes film and restores position');
const ask=host.querySelector('.paper-studio-chapter').querySelectorAll('button')[1];w._paperHash='hash-b';ask.click();
assert(asked.length===1,'stale paper cannot ask a question');
owner.destroyVideoArtifact();assert(!host.children.length&&!film.getAttribute('src')&&pauses&&loads,'teardown releases source and listeners');
w._paperHash='hash-a';delete state.scenes[1].timeline_contract_version;owner.renderVideoArtifact(host,state);
assert(!host.querySelector('.paper-studio-chapter-play'),'legacy planned time never authorizes navigation');
assert(!owner.videoChapterTimings([{timeline_contract_version:'motion-timeline-v1',timeline_start_s:0,timeline_end_s:1}],30),'inconsistent film duration withholds timing');
owner.destroyVideoArtifact();console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/video-artifact.ts', contents=entry) as built:
        result = subprocess.run(['node', '-e', script, str(ROOT), built], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'
