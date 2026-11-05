/** Executable specifications for the studio game engine's pure modules:
 * the fixed-step loop's sleep/keyframe/catch-up semantics and camera math. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
function load(file){
 const js=ts.transpileModule(fs.readFileSync(path.resolve(__dirname,'..',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={};new Function('require','exports',js)(()=>({}),exports);return exports;
}
const {createGameLoop,STEP_MS,MAX_FRAME_MS}=load('engine/loop.ts');
const camera=load('engine/camera.ts');
function scheduler(){
 const frames=new Map(),timers=new Map();let id=0;
 return {frames,timers,
  ports:{frames:{request:fn=>{frames.set(++id,fn);return id;},cancel:i=>frames.delete(i)},timers:{set:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clear:i=>timers.delete(i)}},
  frame(now){const fns=[...frames.values()];frames.clear();fns.forEach(fn=>fn(now));},
  fire(){const entries=[...timers.values()];timers.clear();entries.forEach(entry=>entry.fn());}};
}
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} != ${b}`);

test('the loop advances exact fixed steps of visible time and renders once per frame',()=>{
 const s=scheduler(),steps=[],renders=[];let busy=true;
 const loop=createGameLoop({...s.ports,visible:()=>true,step:dt=>steps.push(dt),render:now=>renders.push(now),nextWake:()=>busy?0:Infinity});
 loop.wake();assert.equal(s.frames.size,1);
 s.frame(1000);assert.equal(steps.length,0,'the first frame after sleep only renders');
 s.frame(1000+STEP_MS*2.5);assert.equal(steps.length,2);assert.ok(steps.every(dt=>dt===STEP_MS));
 s.frame(1000+STEP_MS*3);assert.equal(steps.length,3,'the fractional remainder carries over');
 assert.equal(renders.length,3);
 // A stalled tab replays at most MAX_FRAME_MS of simulation in one burst.
 s.frame(1000+STEP_MS*3+60_000);assert.equal(steps.length,3+Math.round(MAX_FRAME_MS/STEP_MS));
 busy=false;s.frame(70_000);assert.equal(s.frames.size,0);assert.equal(s.timers.size,0,'an idle world owns no callbacks');
 loop.dispose();
});

test('a world with only keyframes sleeps on one timer and resumes without replaying slept time',()=>{
 const s=scheduler();let steps=0;const wake=[0,0,250,Infinity];let call=0;
 const loop=createGameLoop({...s.ports,visible:()=>true,step:()=>{steps++;},render(){},nextWake:()=>wake[Math.min(call++,wake.length-1)]});
 loop.wake();s.frame(0);s.frame(STEP_MS);assert.equal(steps,1);
 s.frame(STEP_MS*2);assert.equal(s.frames.size,0);assert.equal(s.timers.size,1);assert.equal([...s.timers.values()][0].ms,250);
 s.fire();assert.equal(s.frames.size,1);
 s.frame(5000);assert.equal(steps,2,'the keyframe render after a sleep does not step slept time');
 assert.equal(s.frames.size+s.timers.size,0);assert.deepEqual(loop.pending(),{frame:false,timer:false});
 loop.dispose();
});

test('hidden worlds and disposed loops schedule nothing',()=>{
 const s=scheduler();let visible=false;
 const loop=createGameLoop({...s.ports,visible:()=>visible,step(){},render(){},nextWake:()=>0});
 loop.wake();assert.equal(s.frames.size,0);
 visible=true;loop.wake();assert.equal(s.frames.size,1);loop.stop();assert.equal(s.frames.size,0);
 loop.wake();loop.dispose();assert.equal(s.frames.size,0);loop.wake();assert.equal(s.frames.size,0);
});

test('zoom 1 fits the room height inside the HUD frame, centered between the insets',()=>{
 const c=camera.createCamera(960,720);camera.resizeCamera(c,1600,900);
 near(camera.cameraScale(c),1.25);
 const topLeft=camera.worldToScreen(c,{x:0,y:0});near(topLeft.x,200);near(topLeft.y,0);
 camera.setInsets(c,{top:60,bottom:90,left:100,right:0});
 near(camera.cameraScale(c),750/720);
 const center=camera.worldToScreen(c,{x:480,y:360});near(center.x,850);near(center.y,435);
 assert.equal(camera.overflows(c),false);
 const back=camera.screenToWorld(c,camera.worldToScreen(c,{x:123,y:456}));near(back.x,123);near(back.y,456);
});

test('portrait frames overflow horizontally, pan within bounds and keep zoom anchors fixed',()=>{
 const c=camera.createCamera(960,720);camera.resizeCamera(c,390,844);
 assert.equal(camera.overflows(c),true);
 const scale=camera.cameraScale(c),halfWidth=390/scale/2;
 camera.panBy(c,10_000,0);near(c.x,halfWidth);
 camera.panBy(c,-10_000,0);near(c.x,960-halfWidth);
 camera.centerOn(c,{x:100,y:100});near(c.x,halfWidth);near(c.y,360);
 const wide=camera.createCamera(960,720);camera.resizeCamera(wide,1600,900);
 const anchor={x:400,y:300},before=camera.screenToWorld(wide,anchor);
 camera.zoomAt(wide,2,anchor);const after=camera.screenToWorld(wide,anchor);near(after.x,before.x);near(after.y,before.y);
 camera.zoomAt(wide,100,anchor);assert.equal(wide.zoom,camera.MAX_ZOOM);
 camera.zoomAt(wide,.0001,anchor);assert.equal(wide.zoom,1);near(wide.x,480);near(wide.y,360);
 camera.zoomAt(wide,2,anchor);camera.resetCamera(wide);assert.equal(wide.zoom,1);near(wide.x,480);
});

const {JSDOM}=require('jsdom');
function loadTree(name,parent=path.resolve(__dirname,'..','index.ts'),cache=new Map()){
 const full=path.resolve(path.dirname(parent),name.endsWith('.ts')?name:name+'.ts');
 if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(dep=>loadTree(dep,full,cache),exports);return exports;
}
test('the accessible target layer follows the world while HUD insets ease, not only on the first frame',async()=>{
 const d=new JSDOM('<div id="host"></div>',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 const stage=loadTree('engine/stage').mountStage(document.getElementById('host'),{label:'Stage',fontFamily:'sans-serif'});
 stage.setScene({width:960,height:720,backdrop:()=>undefined,step(){},draw(){},nextWake:()=>Infinity,hit:()=>'',activate(){},
  targets:()=>[{id:'desk',label:'Desk',x:400,y:300,width:40,height:20}]});
 const button=()=>document.querySelector('.evo-target[data-target=desk]');
 const settled=async check=>{
  const deadline=Date.now()+20000;
  while(Date.now()<deadline&&!(check()&&!stage.metrics().pending.frame))await new Promise(resolve=>setTimeout(resolve,16));
  assert.ok(check(),'stage settled');
 };
 const expected=()=>camera.worldToScreen(stage.camera,{x:400,y:300});
 await settled(()=>!!button());
 near(parseFloat(button().style.left),expected().x);
 // Live motion eases the inset over many frames; only the frame centre moves.
 stage.setInsets({top:0,right:400,bottom:0,left:0});
 await settled(()=>stage.camera.insets.right===400);
 near(parseFloat(button().style.left),expected().x);near(parseFloat(button().style.top),expected().y);
 assert.ok(Math.abs(parseFloat(button().style.left)-400)>100,'the docked window moved the world');
 stage.dispose();d.window.close();
});
