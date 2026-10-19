"""Video completion waits for real settlement beyond the former polling cliff."""
import os
from pathlib import Path
import subprocess
import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_video_poll_preserves_long_work_and_stops_on_removal():
    script = r'''
const fs = require('node:fs'); const vm = require('node:vm');
const context = {console, runtimeScope:{}, document:{addEventListener:()=>{}}, setTimeout: callback => callback(),
  sessionStorage:{getItem:()=>null,removeItem:()=>{},setItem:()=>{}},
  t:key=>key,debugLog:()=>{}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/upload.js','utf8'),context);
context.renderImagePreviews=()=>{};
(async()=>{
  const entry={name:'long.mp4',video_id:'long',_status:'processing'};
  context.pendingVideos.push(entry); let calls=0;
  context.Api={videos:{status:async()=> ++calls<=181 ? {status:'processing'} :
    {status:'ready',attachment:{attachmentId:'long',sourceUrl:'/video',frameCount:1}}}};
  await context._pollVideoReady(entry);
  if(calls!==182 || entry._status!==undefined || entry.video_id!=='long') throw Error('long processing lost');
  const removed={name:'removed',video_id:'removed'};context.pendingVideos.push(removed);
  context.setTimeout=callback=>{context.pendingVideos.splice(context.pendingVideos.indexOf(removed),1);callback();};
  const previous=calls;
  try {await context._pollVideoReady(removed);throw Error('removal did not stop');}
  catch(error){if(error.message!=='removed')throw error;}
  if(calls!==previous)throw Error('removed attachment still polled');
  const late={name:'late',video_id:'late',_status:'processing'};
  context.pendingVideos.push(late);context.setTimeout=callback=>callback();
  context.Api.videos.status=async()=>{
    context.pendingVideos.splice(context.pendingVideos.indexOf(late),1);
    return {status:'ready',attachment:{attachmentId:'late-new',sourceUrl:'/late'}};
  };
  try {await context._pollVideoReady(late);throw Error('late removal did not stop');}
  catch(error){if(error.message!=='removed')throw error;}
  if(late.video_id!=='late' || late._status!=='processing')throw Error('late response changed a removed owner');

})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([os.environ.get('PLAYWRIGHT_NODEJS_PATH','node'),'-e',script],cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0,result.stdout+result.stderr
