/** Executable specifications for the main office scene (office-scene.ts):
 * every resident pose follows the projected backend view, idle worlds sleep,
 * and the human's avatar is the only character the human steers. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const {JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 const full=path.resolve(path.dirname(parent),name.endsWith('.ts')?name:name+'.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(dep=>/\.(png|webp)$/.test(dep)?{default:'/art.webp'}:load(dep,full),exports);return exports;
}
const {projectStudio}=load('studio/projection');
const {walkable}=load('office-navigation');
const layout=load('office-layout');
const STEP=1000/60;
const names={executor:'Executor',reviewer:'Reviewer',coordinator:'Coordinator'};
const placeLabels={library:'Library',plan:'Plan',work:'Work',review:'Review',delivery:'Delivery'};
const run=(over={})=>({run_id:'goal',objective:'Goal',status:'executing',task_id:'exec',attempt:1,created_at:1,updated_at:1,
 actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},receipts:[],result:'',review:null,error:'',...over});
function setup(){
 const d=new JSDOM('',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 const activated=[];
 const office=load('office-scene').createOfficeScene({copy:k=>k,wake(){},activate:target=>activated.push(target)});
 const scene=office.scene;
 const update=(snapshot,extra={})=>office.update({goalId:'goal',view:projectStudio(snapshot),materials:[],assignmentSeq:0,agents:[],
  names,actorLabels:names,placeLabels,attention:[],...extra});
 const step=(axis={x:0,y:0})=>scene.step(STEP,{axis});
 const drive=(limit=6000)=>{let i=0;for(;i<limit&&scene.nextWake(0,false)===0;i++)step();return i;};
 const until=(fn,limit=6000)=>{for(let i=0;i<limit&&!fn();i++)step();assert.ok(fn(),'condition reached');};
 const target=id=>scene.targets().find(item=>item.id===id);
 const actor=role=>{const t=target('actor:'+role);return {...t.data,x:t.x+t.width/2,y:t.y+t.height};};
 return {d,office,scene,update,step,drive,until,target,actor,activated};
}
const near=(a,b,eps=.5)=>assert.ok(Math.abs(a-b)<=eps,`${a} != ${b}`);

test('an idle office sleeps; a working stage animates only on its own keyframes',()=>{
 const s=setup();
 s.update({});s.drive();
 assert.equal(s.scene.nextWake(0,false),Infinity);
 s.update({run:run()});assert.equal(s.scene.nextWake(0,false),0,'the executor walks to its desk');
 const steps=s.drive();assert.ok(steps>10&&steps<6000);
 const executor=s.actor('executor');
 assert.equal(executor.seated,'true');assert.equal(executor.activity,'working');assert.equal(executor.emote,'work');
 const wake=s.scene.nextWake(0,false);assert.ok(wake>0&&Number.isFinite(wake),'typing emote keyframe, not continuous frames');
 assert.equal(s.scene.nextWake(0,true),Infinity,'reduced motion owns no callbacks');
 s.office.dispose();s.d.window.close();
});

test('new agent materials ride from the desk to the shelf pile; owner materials do not trip',()=>{
 const s=setup(),agent=id=>({material_id:id,origin:'agent'}),user=id=>({material_id:id,origin:'user'});
 const books=()=>s.target('place:library').data.books;
 s.update({run:run()},{materials:[agent('a')]});s.drive();assert.equal(books(),'shelf');
 s.update({run:run()},{materials:[agent('a'),agent('b')]});assert.equal(books(),'shelf,desk');
 s.until(()=>s.actor('executor').carrying==='true');assert.equal(books(),'shelf,carried');
 s.drive();assert.equal(books(),'shelf,shelf');
 assert.equal(s.actor('executor').seated,'true');assert.equal(s.actor('executor').activity,'working');
 s.update({run:run()},{materials:[agent('a'),agent('b'),user('c')]});assert.equal(books(),'shelf,shelf,shelf');
 assert.equal(s.scene.nextWake(0,false)>0,true,'a shelved owner link starts no trip');
 // Removing the middle link keeps the identities of the remaining books.
 s.update({run:run()},{materials:[agent('a'),user('c')]});assert.equal(s.target('place:library').data.materialIds,'a,c');
 // A stage that stops working settles a waiting book on the durable shelf.
 s.update({run:run()},{materials:[agent('a'),user('c'),agent('d')]});assert.equal(books(),'shelf,shelf,desk');
 s.update({run:run({status:'completed',receipts:[{task_id:'exec',stage:'executing',terminal_verdict:'completed'}]})},{materials:[agent('a'),user('c'),agent('d')]});
 assert.equal(books(),'shelf,shelf,shelf');
 s.office.dispose();s.d.window.close();
});

test('a human question gathers the asking role at the door; compute waits keep it at the desk',()=>{
 const s=setup();
 s.update({run:run(),questions:1});s.drive();
 const asking=s.actor('executor');
 near(asking.x,layout.WAIT_POINTS.executor.x);near(asking.y,layout.WAIT_POINTS.executor.y);
 assert.equal(asking.emote,'ask');assert.equal(asking.mode,'waiting');
 s.update({run:run({compute_wait:{operation_id:'job-1',task_id:'exec',parked:true,next_poll_at:0},
  receipts:[{task_id:'exec',stage:'executing',terminal_verdict:'completed'}]})});
 s.drive();
 const waiting=s.actor('executor');
 assert.equal(waiting.seated,'true');assert.equal(waiting.emote,'compute');assert.equal(waiting.mode,'waiting');
 s.office.dispose();s.d.window.close();
});

test('a new committed assignment sends the coordinator to the executor exactly once',()=>{
 const s=setup();
 s.update({run:run()},{assignmentSeq:1});s.drive();
 s.update({run:run()},{assignmentSeq:2});
 s.until(()=>s.actor('coordinator').activity==='talk');
 const talking=s.actor('coordinator');near(talking.x,layout.HANDOFF_POINT.x);near(talking.y,layout.HANDOFF_POINT.y);
 s.drive();assert.equal(s.office.diagnostics().assignmentSeq,'2');
 s.update({run:run()},{assignmentSeq:2});assert.notEqual(s.scene.nextWake(0,false),0,'a poll of the same journal starts no trip');
 s.office.dispose();s.d.window.close();
});

test('swarm visitors enter through the front door, stay capped, and leave when finished',()=>{
 const s=setup();
 const agents=Array.from({length:12},(_,i)=>({id:'v'+i,role:i%2?'coder':'researcher',status:'running'}));
 s.update({run:run()},{agents:agents.slice(0,1)});
 const entering=s.target('visitor:v0');near(entering.y+entering.height,layout.OUTSIDE_POINT.y);
 s.drive();
 const seated=s.target('visitor:v0');near(seated.y+seated.height,layout.VISITOR_DESKS[0].y);
 s.update({run:run()},{agents});s.drive();
 assert.equal(s.scene.targets().filter(item=>item.id.startsWith('visitor:')).length,layout.MAX_VISITORS);
 s.update({run:run()},{agents:[]});
 assert.equal(s.scene.targets().filter(item=>item.id.startsWith('visitor:')).length,0,'departing visitors are no longer targets');
 assert.notEqual(s.office.diagnostics().visitors,'');
 s.drive();assert.equal(s.office.diagnostics().visitors,'');
 s.office.dispose();s.d.window.close();
});

test('reduced motion settles every beat at once and schedules nothing',()=>{
 const s=setup();s.scene.setStill(true);
 s.update({run:run()},{materials:[{material_id:'a',origin:'agent'}],agents:[{id:'v',role:'coder',status:'running'}]});
 assert.equal(s.actor('executor').seated,'true');assert.equal(s.office.diagnostics().chairs,'in,in');
 const visitor=s.target('visitor:v');near(visitor.y+visitor.height,layout.VISITOR_DESKS[0].y);
 assert.equal(s.scene.nextWake(0,true),Infinity);
 s.office.dispose();s.d.window.close();
});

test('furniture and residents are targets; pointer activation also walks your avatar there',()=>{
 const s=setup();s.update({run:run()});s.drive();
 assert.equal(s.scene.hit({x:480,y:60}),'place:library');
 assert.equal(s.scene.hit({x:727,y:560}),'place:delivery');
 const executor=s.target('actor:executor');
 assert.equal(s.scene.hit({x:executor.x+executor.width/2,y:executor.y+executor.height/2}),'actor:executor');
 s.scene.activate('place:library','pointer');assert.deepEqual(s.activated.at(-1),{kind:'place',place:'library'});
 s.drive();assert.equal(s.office.diagnostics().player,`${layout.HOTSPOTS.library.approach.x},${layout.HOTSPOTS.library.approach.y}`);
 s.scene.activate('place:delivery','keyboard');assert.deepEqual(s.activated.at(-1),{kind:'place',place:'delivery'});
 assert.equal(s.office.diagnostics().player,`${layout.HOTSPOTS.library.approach.x},${layout.HOTSPOTS.library.approach.y}`,'keyboard activation does not move you');
 s.scene.interact();assert.deepEqual(s.activated.at(-1),{kind:'place',place:'library'},'E interacts with the nearest furniture');
 s.scene.activate('actor:reviewer','pointer');assert.deepEqual(s.activated.at(-1),{kind:'actor',role:'reviewer'});
 assert.equal(s.target('actor:reviewer').pressed,true);
 s.office.dispose();s.d.window.close();
});

test('keyboard steering slides along furniture and never enters it',()=>{
 const s=setup();s.update({});
 for(const axis of [{x:-1,y:0},{x:0,y:-1},{x:-.7,y:-.7},{x:1,y:0},{x:0,y:1}]){
  for(let i=0;i<400;i++){s.step(axis);const [x,y]=s.office.diagnostics().player.split(',').map(Number);assert.ok(walkable({x,y}),`${x},${y}`);}
 }
 s.office.dispose();s.d.window.close();
});

test('research tool calls send the working executor to the shelf and back to its desk',()=>{
 const s=setup();s.update({run:run()});s.drive();
 s.office.setToolBubble('executor','web_search');
 s.until(()=>s.actor('executor').activity==='research');
 const researching=s.actor('executor');near(researching.x,layout.SHELF_POINT.x);near(researching.y,layout.SHELF_POINT.y);
 assert.equal(researching.tool,'web_search');
 s.office.setToolBubble('executor','');s.drive();
 assert.equal(s.actor('executor').seated,'true');assert.equal(s.actor('executor').activity,'working');
 s.office.dispose();s.d.window.close();
});

test('a floor tap keeps frames only while your avatar walks, and none at all under reduced motion',()=>{
 const s=setup();s.update({});s.drive();
 const [x,y]=s.office.diagnostics().player.split(',').map(Number);
 s.scene.tapFloor({x:x+30,y});assert.equal(s.scene.nextWake(0,false),0);
 s.until(()=>s.office.diagnostics().player===`${x+30},${y}`);
 assert.notEqual(s.scene.nextWake(0,false),0,'arrival ends the frames');
 s.scene.setStill(true);s.scene.tapFloor({x,y});
 assert.equal(s.office.diagnostics().player,`${x},${y}`);
 assert.equal(s.scene.nextWake(0,true),Infinity,'an instant move schedules nothing');
 s.office.dispose();s.d.window.close();
});
