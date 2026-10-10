const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const {JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 const full=path.resolve(path.dirname(parent),name+'.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/game-art.webp'}:name.endsWith('/i18n')?{t:k=>k}:load(name,full),exports);return exports;
}
const {findOfficePath,walkable}=load('office-navigation');
test('office routes go around desks and reject furniture and exterior destinations',()=>{
 const route=findOfficePath({x:430,y:350},{x:250,y:290});assert.ok(route.length>5);
 for(const point of route)assert.ok(walkable(point));
 for(const point of [{x:250,y:190},{x:10,y:500},{x:100,y:500},{x:9000,y:-1}])assert.deepEqual(findOfficePath({x:430,y:350},point),[]);
 const across=findOfficePath({x:430,y:170},{x:250,y:290});
 assert.ok(across.some(p=>p.y>=270));assert.ok(across.every(walkable));
 // The right-wall door stays connected to every hot desk, station and break point.
 for(const point of [{x:480,y:470},{x:560,y:530},{x:400,y:470},{x:700,y:350},{x:250,y:288},{x:750,y:288},{x:178,y:330},{x:740,y:270},{x:440,y:590},{x:530,y:590},{x:610,y:480}]){
   assert.ok(findOfficePath({x:895,y:190},point).length>0,'door route to '+point.x+','+point.y);
   assert.ok(findOfficePath(point,{x:895,y:190}).length>0,'return route from '+point.x+','+point.y);
 }
  // The workstation fronts and the material trip stops stay reachable.
  for(const point of [{x:250,y:270},{x:740,y:270},{x:230,y:350},{x:810,y:290}])
   assert.ok(findOfficePath({x:430,y:355},point).length>0,'route to '+point.x+','+point.y);
});
function harness(){
 const d=new JSDOM('<div id="scene"></div><div id="controls"></div>',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 const frames=new Map(),timers=new Map();let id=0;
 window.requestAnimationFrame=fn=>{frames.set(++id,fn);return id;};window.cancelAnimationFrame=id=>frames.delete(id);
 window.setTimeout=fn=>{timers.set(++id,fn);return id;};window.clearTimeout=id=>timers.delete(id);
 const host=document.getElementById('scene');
 let now=0;
 const step=(dt=50)=>{now+=dt;const fns=[...frames.values()];frames.clear();fns.forEach(fn=>fn(now));};
 const drive=()=>{for(let guard=0;frames.size&&guard<2000;guard++)step();};
 const driveUntil=fn=>{for(let guard=0;frames.size&&guard<2000&&!fn();guard++)step();};
 return {d,frames,timers,host,step,drive,driveUntil};
}
test('animation owns bounded callbacks, stops hidden/offscreen, and releases canvas on unmount',()=>{
 const {d,frames,timers,host}=harness();
 let intersection;
 global.IntersectionObserver=class {constructor(fn){intersection=fn;}observe(){}disconnect(){}};
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 const roles=[{name:'Execute',state:'working',taskId:'real-task',working:true,waiting:false},{name:'Review',state:'idle',taskId:'',working:false,waiting:false}];
 scene.update(roles);scene.update(roles);assert.equal(frames.size,1);assert.ok(timers.size<=1);
 Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new window.Event('visibilitychange'));
 assert.equal(frames.size,0);assert.equal(timers.size,0);
 Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new window.Event('visibilitychange'));assert.equal(frames.size,1);
 intersection([{isIntersecting:false}]);assert.equal(frames.size,0);assert.equal(timers.size,0);
 intersection([{isIntersecting:true}]);assert.equal(frames.size,1);
 document.querySelector('.evo-scene-pause').click();assert.equal(frames.size,0);assert.equal(timers.size,0);
  scene.dispose();assert.equal(host.querySelector('canvas').width,0);assert.equal(frames.size+timers.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('material books ride the executor from desk to shelf, bounded',()=>{
  const {d,frames,timers,host,drive,driveUntil}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const executor=host.querySelector('.evo-person');
  const roles=[{name:'Execute',state:'working',taskId:'real-task',working:true,waiting:false},{name:'Review',state:'idle',taskId:'',working:false,waiting:false}];
  const agent=id=>({material_id:id,origin:'agent'});
  const user=id=>({material_id:id,origin:'user'});
  // The first update baselines the count: the found book is already on the shelf.
  scene.update(roles,{goalId:'goal-a',materials:[agent('a')]});drive();
  assert.equal(executor.dataset.seated,'true');assert.equal(executor.dataset.activity,'working');
  assert.equal(host.dataset.books,'shelf');
  // A new agent-collected material waits on the desk, then really gets carried over.
  scene.update(roles,{goalId:'goal-a',materials:[agent('a'),agent('b')]});
  assert.equal(host.dataset.books,'shelf,desk');
  driveUntil(()=>executor.dataset.carrying==='true');
  assert.equal(host.dataset.books,'shelf,carried');
  drive();
  assert.equal(host.dataset.books,'shelf,shelf');
  assert.equal(executor.dataset.carrying,'false');assert.equal(executor.dataset.seated,'true');
  assert.equal(executor.dataset.activity,'working');assert.equal(frames.size,0);assert.ok(timers.size<=1);
  // An unchanged count never re-trips; another new material does.
  scene.update(roles,{goalId:'goal-a',materials:[agent('a'),agent('b')]});assert.equal(frames.size,0);
  // Owner-added sources are real shelf links, but were not fetched by this agent.
  scene.update(roles,{goalId:'goal-a',materials:[agent('a'),agent('b'),user('c')]});
  assert.equal(host.dataset.books,'shelf,shelf,shelf');assert.equal(frames.size,0);
  scene.update(roles,{goalId:'goal-a',materials:[agent('a'),agent('b'),user('c'),agent('d')]});assert.ok(frames.size>0);drive();
  assert.equal(host.dataset.books,'shelf,shelf,shelf,shelf');
  // Removing a middle link preserves the identities of the remaining books.
  scene.update(roles,{goalId:'goal-a',materials:[agent('a'),user('c'),agent('d')]});
  assert.equal(host.dataset.materialIds,'a,c,d');assert.equal(host.dataset.books,'shelf,shelf,shelf');
  // Switching goals baselines its own shelf; a finished goal keeps its books.
  scene.update(roles,{goalId:'goal-b',materials:[user('other')]});
  assert.equal(host.dataset.materialIds,'other');assert.equal(host.dataset.books,'shelf');
  scene.update([{...roles[0],working:false},roles[1]],{goalId:'goal-b',materials:[user('other')]});drive();
  assert.equal(host.dataset.books,'shelf');
  scene.dispose();assert.equal(frames.size+timers.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('tool events send the executor to the research shelf or coordinator without inventing materials',()=>{
 const {d,host,drive}=harness();
 global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 const actor=host.querySelector('.evo-person[data-actor="0"]');
 const roles=[{name:'Execute',state:'working',taskId:'task',working:true,waiting:false}];
 scene.update(roles,{goalId:'goal',materials:[]});drive();
 scene.setToolBubble(0,'Searching','web_search');drive();
 assert.equal(actor.dataset.activity,'research');
 assert.equal(actor.querySelector('.evo-person-bubble').dataset.kind,'research');
 assert.equal(host.dataset.books,'');
 scene.setToolBubble(0,'');drive();
 assert.equal(actor.dataset.seated,'true');
 scene.setToolBubble(0,'Coordinating','evolution_coordinate');drive();
 assert.equal(actor.dataset.activity,'talk');
 scene.setToolBubble(0,'');drive();
 assert.equal(actor.dataset.seated,'true');
 scene.dispose();delete global.IntersectionObserver;d.window.close();
});
test('a new committed assignment sends the coordinator to the executor once',()=>{
  const {d,frames,host,drive,driveUntil}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const roles=[{name:'Execute',state:'working',taskId:'t1',working:true,waiting:false},
    {name:'Review',state:'idle',taskId:'',working:false,waiting:false},
    {name:'Lead',state:'thinking',taskId:'t2',working:true,waiting:false}];
  scene.update(roles,{goalId:'goal-a',assignmentSeq:4});drive();
  assert.equal(host.dataset.assignmentSeq,'4');
  scene.update(roles,{goalId:'goal-a',assignmentSeq:5});
  driveUntil(()=>host.querySelector('[data-actor="2"]').dataset.activity==='talk');
  assert.equal(host.querySelector('[data-actor="2"]').dataset.activity,'talk');
  drive();assert.equal(host.dataset.assignmentSeq,'5');
  scene.update(roles,{goalId:'goal-a',assignmentSeq:5});assert.equal(frames.size,0);
  scene.dispose();delete global.IntersectionObserver;d.window.close();
});
test('actors pull chairs, sit and roll in, then roll out and push chairs back',()=>{
  const {d,frames,host,drive,driveUntil}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const executor=host.querySelector('.evo-person');
  assert.equal(host.dataset.chairs,'in,in');
  const roles=[{name:'Execute',state:'working',taskId:'t1',working:true,waiting:false}];
  scene.update(roles);
  driveUntil(()=>host.dataset.chairActions.startsWith('pull'));
  assert.equal(executor.dataset.seated,'false');
  driveUntil(()=>host.dataset.chairActions.startsWith('roll-in'));
  assert.equal(executor.dataset.seated,'true');
  assert.equal(host.dataset.chairs,'moving,in');
  drive();
  assert.equal(executor.dataset.seated,'true');
  assert.equal(host.dataset.chairs,'in,in');
  scene.update([{name:'Execute',state:'ask',taskId:'t1',working:false,waiting:true}]);
  driveUntil(()=>host.dataset.chairActions.startsWith('roll-out'));
  assert.equal(executor.dataset.seated,'true');
  driveUntil(()=>host.dataset.chairActions.startsWith('push'));
  assert.equal(executor.dataset.seated,'false');
  drive();
  assert.equal(executor.dataset.activity,'waiting');
  assert.equal(host.dataset.chairs,'in,in');
  // The chair itself is an accessible target that can invite the actor back.
  host.querySelector('[data-chair="0"]').click();drive();
  assert.equal(executor.dataset.seated,'true');
  assert.equal(host.dataset.chairs,'in,in');
  scene.dispose();assert.equal(frames.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('an interrupted chair pull restores furniture before the new route',()=>{
  const {d,host,drive,driveUntil}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const actor=host.querySelector('[data-actor="0"]');
  scene.update([{name:'Execute',state:'working',taskId:'t',working:true,waiting:false}]);
  driveUntil(()=>host.dataset.chairActions.startsWith('pull'));
  scene.update([{name:'Execute',state:'waiting',taskId:'t',working:false,waiting:true}]);drive();
  assert.equal(host.dataset.chairs,'in,in');assert.equal(actor.dataset.seated,'false');
  assert.equal(actor.dataset.activity,'waiting');
  scene.dispose();delete global.IntersectionObserver;d.window.close();
});
test('paused chair actions settle immediately without animation callbacks',()=>{
  const {d,host,frames,timers}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  document.querySelector('.evo-scene-pause').click();
  const chair=host.querySelector('[data-chair="0"]'),actor=host.querySelector('[data-actor="0"]');
  chair.click();assert.equal(actor.dataset.seated,'true');assert.equal(host.dataset.chairs,'in,in');
  chair.click();assert.equal(actor.dataset.seated,'false');assert.equal(host.dataset.chairs,'in,in');
  assert.equal(frames.size,0);assert.equal(timers.size,0);
  scene.dispose();delete global.IntersectionObserver;d.window.close();
});
test('actors sit at their stations with distinct sprites and role activities',()=>{
  const {d,frames,host,drive}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const buttons=[...host.querySelectorAll('.evo-person')];
  // Every role owns a distinct sprite from the six-character atlas.
  assert.deepEqual(buttons.map(b=>b.dataset.sprite),['0','1','2']);
  assert.deepEqual(buttons.map(b=>b.dataset.model),['executor','reviewer','coordinator']);
  const working=[{name:'Execute',state:'working',taskId:'t1',working:true,waiting:false},{name:'Review',state:'idle',taskId:'',working:false,waiting:false},{name:'Lead',state:'thinking',taskId:'t2',working:true,waiting:false}];
  scene.update(working);drive();
  // The executor sits at the desk; the coordinator stands thinking at the plan board.
  assert.equal(buttons[0].dataset.seated,'true');assert.equal(buttons[0].dataset.activity,'working');
  assert.equal(buttons[2].dataset.seated,'false');assert.equal(buttons[2].dataset.activity,'thinking');
  // A human question stands the actor up at the front with the waiting marker.
  scene.update([{...working[0],working:false,waiting:true},working[1],working[2]]);drive();
  assert.equal(buttons[0].dataset.seated,'false');assert.equal(buttons[0].dataset.activity,'waiting');
  scene.dispose();assert.equal(frames.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('idle actors remain stationary with neutral gait and zero callbacks',()=>{
  const {d,frames,timers,host,drive}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const buttons=[...host.querySelectorAll('.evo-person')];
  const positions=buttons.map(b=>[b.style.left,b.style.top]);
  assert.equal(timers.size+frames.size,0);drive();
  scene.update([]);drive();
  assert.deepEqual(buttons.map(b=>[b.style.left,b.style.top]),positions);
  assert.ok(buttons.every(b=>b.dataset.frame==='1'&&b.dataset.activity===''));
  assert.equal(timers.size+frames.size,0);
  scene.dispose();assert.equal(frames.size+timers.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('swarm visitors enter through the door and leave when terminal',()=>{
  const {d,frames,host,drive}=harness();
  global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const roles=[{name:'Execute',state:'working',taskId:'real-task',working:true,waiting:false}];
  const door=host.querySelector('.evo-door');
  // A live agent spawns at the door, which opens, then walks to a hot desk.
  scene.update(roles,{agents:[{id:'a1',role:'researcher',status:'running'}],agentsTaskId:'real-task'});
  assert.equal(host.querySelectorAll('.evo-visitor').length,1);
  assert.equal(door.dataset.open,'true');
  drive();assert.ok(frames.size===0);
  // The visitor really left the door and settled at the first hot desk.
  assert.ok(parseFloat(host.querySelector('.evo-visitor').style.left)<60,'visitor left the door corridor');
  // Roster updates keep the visitor; a second live agent joins, capped desks.
  scene.update(roles,{agents:[{id:'a1',role:'researcher',status:'running'},{id:'a2',role:'coder',status:'pending'}],agentsTaskId:'real-task'});
  assert.equal(host.querySelectorAll('.evo-visitor').length,2);drive();
  // Visitors cycle the atlas's spare characters, never the role sprites.
  assert.deepEqual([...host.querySelectorAll('.evo-visitor')].map(v=>v.dataset.sprite),['3','4']);
  assert.equal(host.querySelector('.evo-visitor').dataset.activity,'working');
  // Terminal status sends the visitor back out through the door; it vanishes.
  scene.update(roles,{agents:[{id:'a1',role:'researcher',status:'done'},{id:'a2',role:'coder',status:'running'}],agentsTaskId:'real-task'});
  drive();assert.equal(host.querySelectorAll('.evo-visitor').length,1);
  assert.equal(host.querySelector('.evo-visitor').dataset.visitor,'a2');
  // An empty roster dismisses the rest; disposal releases every timer/frame.
  scene.update(roles,{agents:[],agentsTaskId:''});drive();
  assert.equal(host.querySelectorAll('.evo-visitor').length,0);
  scene.dispose();assert.equal(frames.size,0);
  delete global.IntersectionObserver;d.window.close();
});

test('chair-to-stand starts at the prior pose and interrupted seating preserves its foot anchor',()=>{
 const {d,host,step,drive,driveUntil,frames}=harness();
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 const actor=host.querySelector('[data-actor="0"]');
 const working={name:'Execute',state:'working',taskId:'t',working:true,waiting:false};
 scene.update([working]);driveUntil(()=>Number(actor.dataset.seatAmount)>.2&&Number(actor.dataset.seatAmount)<.9);
 const seat=Number(actor.dataset.seatAmount),before=[actor.style.left,actor.style.top];
 scene.update([{...working,working:false,waiting:true}]);
 assert.equal(Number(actor.dataset.seatAmount),seat);assert.deepEqual([actor.style.left,actor.style.top],before);
 driveUntil(()=>actor.dataset.phase==='stand');
 assert.equal(Number(actor.dataset.seatAmount),seat,'new beat cannot consume prior chair-roll time');
 const feet=[actor.style.left,actor.style.top];step();
 assert.ok(Number(actor.dataset.seatAmount)<seat);assert.deepEqual([actor.style.left,actor.style.top],feet);
 drive();assert.equal(actor.dataset.activity,'waiting');assert.equal(actor.dataset.seatAmount,'0.000');
 assert.equal(frames.size,0);scene.dispose();d.window.close();
});
test('hidden seat transitions keep visible-time progress and do not catch up on resume',()=>{
 const {d,host,step,drive,driveUntil,frames,timers}=harness();
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 const actor=host.querySelector('[data-actor="0"]');
 scene.update([{name:'Execute',state:'working',taskId:'t',working:true,waiting:false}]);
 driveUntil(()=>Number(actor.dataset.seatAmount)>.1&&Number(actor.dataset.seatAmount)<.5);
 Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new window.Event('visibilitychange'));
 const seat=Number(actor.dataset.seatAmount);for(let i=0;i<200;i++)step();
 assert.equal(Number(actor.dataset.seatAmount),seat);assert.equal(frames.size+timers.size,0);
 Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new window.Event('visibilitychange'));step();
 assert.ok(Number(actor.dataset.seatAmount)>seat);assert.ok(Number(actor.dataset.seatAmount)<1);
 drive();assert.equal(actor.dataset.seated,'true');scene.dispose();d.window.close();
});
test('hidden roster updates schedule no callbacks and dispose ignores late inputs',()=>{
 const {d,host,frames,timers}=harness();
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new window.Event('visibilitychange'));
 scene.update([],{agents:[{id:'hidden',role:'researcher',status:'running'}]});
 assert.equal(frames.size+timers.size,0);assert.equal(host.querySelector('.evo-door').dataset.open,'false');
 scene.dispose();scene.update([],{agents:[{id:'late',role:'writer',status:'running'}]});scene.setToolBubble(0,'late','web_search');
 assert.equal(host.querySelectorAll('.evo-visitor').length,0);assert.equal(frames.size+timers.size,0);d.window.close();
});
test('roster churn stays capped while departures drain and admits latest live visitors without another poll',()=>{
 const {d,host,drive,frames}=harness();
 const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
 const roster=prefix=>Array.from({length:8},(_,i)=>({id:prefix+i,role:'researcher',status:'running'}));
 scene.update([],{agents:roster('old')});drive();
 for(let i=0;i<4;i++){
   scene.update([],{agents:roster('new'+i)});
   assert.ok(host.querySelectorAll('.evo-visitor').length<=8,'leaving actors count toward the cap');
 }
 drive();assert.equal(frames.size,0);
 assert.deepEqual([...host.querySelectorAll('.evo-visitor')].map(v=>v.dataset.visitor).sort(),roster('new3').map(v=>v.id));
 scene.dispose();d.window.close();
});

test('approach, turn, sit and stand keep a continuous foot anchor through interruption and hidden time',()=>{
  const {d,frames,timers,host,step,drive,driveUntil}=harness();
  let intersection;
  global.IntersectionObserver=class {constructor(fn){intersection=fn;}observe(){}disconnect(){}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  const actor=host.querySelector('.evo-person');
  const position=()=>[parseFloat(actor.style.left)*9.6,parseFloat(actor.style.top)*7.2];
  const roles=[{name:'Execute',state:'working',taskId:'real-task',working:true,waiting:false}];
  scene.update(roles);driveUntil(()=>actor.dataset.phase==='sit'&&Number(actor.dataset.seatAmount)>0);
  assert.equal(actor.dataset.direction,'3');assert.equal(actor.dataset.frame,'1');
  const start=position(),pose=actor.dataset.seatAmount;
  assert.ok(Number(pose)>0&&Number(pose)<1);
  intersection([{isIntersecting:false}]);
  assert.equal(frames.size+timers.size,0);
  for(let i=0;i<100;i++)step();
  assert.deepEqual(position(),start);assert.equal(actor.dataset.seatAmount,pose);
  intersection([{isIntersecting:true}]);
  assert.deepEqual(position(),start);assert.equal(actor.dataset.seatAmount,pose);
  scene.setToolBubble(0,'Read','web_search');
  assert.deepEqual(position(),start,'interrupt does not teleport to station');
  drive();assert.equal(actor.dataset.seated,'false');assert.equal(actor.dataset.frame,'1');
  scene.setToolBubble(0,'','');drive();assert.equal(actor.dataset.seated,'true');
  const settled=position();assert.ok(Math.abs(settled[0]-205)<1e-7);assert.ok(Math.abs(settled[1]-288)<1e-7);
  scene.dispose();assert.equal(frames.size+timers.size,0);
  delete global.IntersectionObserver;d.window.close();
});
test('toggling reduced motion during a chair beat settles once without stranded callbacks',()=>{
  const {d,frames,timers,host,driveUntil}=harness();
  let preference;const media={matches:false,addEventListener:(_,fn)=>preference=fn,removeEventListener:()=>{}};
  window.matchMedia=()=>media;
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  scene.update([{name:'Execute',state:'working',taskId:'real-task',working:true,waiting:false}]);
  const actor=host.querySelector('.evo-person');
  driveUntil(()=>actor.dataset.phase==='sit'&&Number(actor.dataset.seatAmount)>0);
  media.matches=true;preference();
  assert.equal(actor.dataset.seated,'true');assert.equal(actor.dataset.seatAmount,'1.000');
  assert.equal(frames.size+timers.size,0);
  media.matches=false;preference();assert.equal(frames.size+timers.size,0);
  scene.dispose();d.window.close();
});
test('identical snapshots skip canvas paints and late image callbacks cannot resurrect disposed scenes',()=>{
  const {d,host,frames,timers}=harness();
  let paints=0;
  window.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:(_,key)=>key==='clearRect'?()=>paints++:()=>{}});
  const images=[];
  window.Image=class {constructor(){this.src='';this.complete=false;this.naturalWidth=0;images.push(this);}};
  const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
  for(let i=0;i<100;i++)scene.update([]);
  assert.equal(paints,1);assert.ok(scene.metrics().skippedPaints>=100);
  const late=images.map(i=>i.onload).filter(Boolean);
  scene.dispose();scene.dispose();late.forEach(fn=>fn());scene.update([]);
  assert.equal(host.querySelector('canvas').width,0);assert.equal(frames.size+timers.size,0);assert.equal(paints,1);
  d.window.close();
});

test('visible movement maintains 30 Hz paint pacing at 60, 120 and 144 Hz without fractional drift',()=>{
 for(const hz of [60,120,144]){
   const {d,host,step}=harness();
   window.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>{}});
   const scene=load('office-scene').mountOfficeScene(host,document.getElementById('controls'),{copy:k=>k,openTask:()=>{},askHuman:()=>{}});
   scene.update([{name:'Execute',state:'working',taskId:'t',working:true,waiting:false}]);
   const before=scene.metrics().paints;
   for(let i=0;i<hz;i++)step(1000/hz);
   assert.equal(scene.metrics().paints-before,30,`${hz} Hz display must not starve 30 Hz renderer`);
   scene.dispose();d.window.close();
 }
});
