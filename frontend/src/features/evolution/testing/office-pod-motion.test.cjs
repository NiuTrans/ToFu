/** Executable specifications for the department-room scene (agent-pod.ts) on
 * the shared stage: first sight settles, a same-agent status change plays one
 * bounded chair beat, and stationary rooms schedule only emote keyframes. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const {load}=require('./office-runtime-loader.cjs');
const STEP=1000/60;
function harness(){
  const dom=new JSDOM('',{pretendToBeVisual:true});
  Object.assign(global,{window:dom.window,document:dom.window.document});
  window.HTMLCanvasElement.prototype.getContext=()=>null;
  const images=[];
  window.Image=class{constructor(){const image=document.createElement('img');images.push(image);return image;}};
  let opened=0;
  const pod=load('agent-pod').createPodScene({wake(){},openMember:()=>{opened++;},statusLabel:status=>status,roleLabel:role=>`title:${role}`});
  const update=status=>pod.update({id:'agent-one',role:'engineer',status,objective:'Real assignment'},{theme:'engineering',accent:0});
  const member=()=>pod.scene.targets()[0];
  const drive=()=>{let i=0;for(;i<4000&&pod.scene.nextWake(0,false)===0;i++)pod.scene.step(STEP,{axis:{x:0,y:0}});return i;};
  return {dom,images,pod,update,member,drive,opened:()=>opened};
}
test('a same-agent status change plays one bounded chair beat and is stationary after arrival',()=>{
  const h=harness();
  h.update('pending');assert.equal(h.member().data.phase,'idle','first sight settles immediately');
  assert.equal(h.member().label,'title:engineer · pending','the member is named by its localized job title');
  assert.ok(h.images.some(image=>image.getAttribute('src')),'the selected room art loads on entry');
  assert.ok(h.pod.scene.nextWake(0,false)>0,'a pending agent shows only its hourglass keyframes');
  h.update('running');assert.equal(h.member().data.beat,'arrive');assert.equal(h.pod.scene.nextWake(0,false),0);
  const steps=h.drive();assert.ok(steps>5&&steps<4000);
  assert.equal(h.member().data.seated,'true');assert.equal(h.member().data.beat,'');
  h.update('running');assert.notEqual(h.pod.scene.nextWake(0,false),0,'an unchanged roster replays nothing');
  h.update('done');assert.equal(h.member().data.beat,'leave');h.drive();
  assert.equal(h.member().data.seated,'false');assert.equal(h.pod.scene.nextWake(0,false),Infinity);
  h.pod.scene.activate('member','keyboard');assert.equal(h.opened(),1);
  h.pod.update(null,{theme:'general',accent:0});assert.deepEqual(h.pod.scene.targets(),[]);assert.equal(h.pod.scene.hit({x:480,y:478}),'');
  h.pod.dispose();h.dom.window.close();
});
test('reduced motion settles a live beat at once; disposal ignores late art callbacks',()=>{
  const h=harness();h.update('pending');h.update('running');
  for(let i=0;i<10;i++)h.pod.scene.step(STEP,{axis:{x:0,y:0}});
  h.pod.scene.setStill(true);
  assert.equal(h.member().data.seated,'true');assert.equal(h.pod.scene.nextWake(0,true),Infinity);
  const callbacks=h.images.map(image=>image.onload).filter(Boolean);
  h.pod.dispose();callbacks.forEach(fn=>fn());h.update('done');h.pod.dispose();
  h.dom.window.close();
});
