const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const {JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 const full=path.resolve(path.dirname(parent),name+'.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/people.png'}:name.endsWith('/i18n')?{t:k=>k}:load(name,full),exports);return exports;
}
function harness(){
 const d=new JSDOM(`<section class="evo-office"><div class="evo-office-stage"><img><div class="evo-office-live"></div><div class="evo-office-objects"></div></div></section><div class="evo-game-dock"><div class="evo-place-shortcuts"></div><div class="evo-office-controls"></div></div>`,{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 const frames=new Map();let id=0;
 window.requestAnimationFrame=fn=>{frames.set(++id,fn);return id;};window.cancelAnimationFrame=id=>frames.delete(id);
 return {d,frames,office:document.querySelector('.evo-office'),dock:document.querySelector('.evo-game-dock')};
}
const agents=[
 {id:'a1',role:'researcher',status:'running',objective:'比较评测方法'},
 {id:'a2',role:'coder',status:'pending',objective:'实现原型'},
];
test('six rooms remain discoverable without a roster and expose live occupancy',()=>{
 const {d,office,dock}=harness();
 const rooms=load('office-rooms').mountOfficeRooms(office,dock,{copy:k=>k,openTask:()=>{}});
 rooms.update([],'');
 assert.equal(rooms.bar.hidden,false);
 assert.equal(office.dataset.room,'main');
 rooms.update(agents,'task-1');
 assert.equal(rooms.bar.hidden,false);
 const tabs=[...rooms.bar.querySelectorAll('.evo-room-tab')];
 assert.equal(tabs.length,6);
 assert.equal(tabs[0].querySelector('strong').textContent,'mainRoom');
 assert.deepEqual(tabs.slice(1).map(t=>t.dataset.room),['research','engineering','review','writing','general']);
 assert.equal(tabs[1].querySelector('.evo-room-dot').dataset.status,'running');
 rooms.dispose();d.window.close();
});
test('entering a sub-agent room swaps the stage and scopes the brief to that agent',()=>{
 const {d,frames,office,dock}=harness();
 global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
 const rooms=load('office-rooms').mountOfficeRooms(office,dock,{copy:k=>k,openTask:()=>{}});
 rooms.update(agents,'task-1');
 rooms.bar.querySelectorAll('.evo-room-tab')[1].click();
 assert.equal(rooms.activeRoom(),'research');
 assert.equal(office.dataset.room,'pod');
  for(const sel of ['.evo-office-live','.evo-office-objects'])
   assert.ok(office.querySelector(sel).hidden,sel+' hidden in the pod');
  assert.ok(dock.querySelector('.evo-place-shortcuts').hidden);
  assert.equal(dock.querySelector('.evo-office-controls').hidden,false);
  assert.ok(office.querySelector('.evo-office-stage>img').hidden);
 const pod=office.querySelector('.evo-office-pod');
 assert.equal(pod.hidden,false);
 assert.equal(pod.dataset.status,'running');
 assert.equal(pod.dataset.theme,'research');
 // A running agent animates, bounded to one frame callback.
 assert.ok(frames.size<=1);
 // The brief card shows only the agent's own role, status and objective.
 assert.equal(rooms.inspector.querySelector('.evo-pod-brief strong').textContent,'researcher');
 assert.equal(rooms.inspector.querySelector('.evo-pod-status').textContent,'running');
 assert.equal(rooms.inspector.querySelector('.evo-pod-objective').textContent,'比较评测方法');
 assert.equal(rooms.inspector.querySelector('.evo-pod-brief small').textContent,'agentRoomScope');
 // The actor button stays keyboard-reachable with a truthful label.
 assert.equal(pod.querySelector('.evo-pod-person').getAttribute('aria-label'),'researcher · running');
 // Back to the main office restores every main-scene element.
 rooms.bar.querySelectorAll('.evo-room-tab')[0].click();
 assert.equal(office.dataset.room,'main');
  for(const sel of ['.evo-office-live','.evo-office-objects'])
   assert.equal(office.querySelector(sel).hidden,false,sel+' restored');
  for(const sel of ['.evo-place-shortcuts','.evo-office-controls'])
   assert.equal(dock.querySelector(sel).hidden,false,sel+' restored');
  assert.equal(office.querySelector('.evo-office-stage>img').hidden,false);
 assert.equal(pod.hidden,true);
 rooms.bar.querySelectorAll('.evo-room-tab')[2].click();
 assert.equal(pod.dataset.theme,'engineering');
 rooms.dispose();assert.equal(frames.size,0);
 delete global.IntersectionObserver;d.window.close();
});
test('a departed agent leaves an empty room and same-role members remain individually selectable',()=>{
 const {d,office,dock}=harness();
 global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
 const rooms=load('office-rooms').mountOfficeRooms(office,dock,{copy:k=>k,openTask:()=>{}});
 rooms.update(agents,'task-1');
 rooms.bar.querySelectorAll('.evo-room-tab')[2].click();
 assert.equal(rooms.activeRoom(),'engineering');
 rooms.update([agents[0]],'task-1');
 assert.equal(rooms.activeRoom(),'engineering');
 assert.equal(office.dataset.room,'pod');
 assert.equal(office.querySelector('.evo-pod-person').hidden,true);
 assert.equal(rooms.bar.querySelectorAll('.evo-room-tab').length,6);
 // Matching roles share a room, with separate identity-bound briefs.
 rooms.update([{id:'b1',role:'coder',status:'running',objective:'First task'},{id:'b2',role:'coder',status:'running',objective:'Second task'}],'task-1');
 const members=rooms.inspector.querySelectorAll('[data-member]');assert.equal(members.length,2);
 members[1].click();assert.equal(rooms.inspector.querySelector('.evo-pod-objective').textContent,'Second task');
 rooms.dispose();delete global.IntersectionObserver;d.window.close();
});
test('a terminal agent keeps a visitable room with a finished brief board',()=>{
 const {d,frames,office,dock}=harness();
 global.IntersectionObserver=class {constructor(){}observe(){}disconnect(){}};
 const rooms=load('office-rooms').mountOfficeRooms(office,dock,{copy:k=>k,openTask:()=>{}});
 rooms.update([{id:'a1',role:'researcher',status:'done',objective:'比较评测方法'}],'task-1');
 rooms.bar.querySelectorAll('.evo-room-tab')[1].click();
 const pod=office.querySelector('.evo-office-pod');
 assert.equal(pod.dataset.status,'done');
 assert.equal(rooms.inspector.querySelector('.evo-pod-status').textContent,'completed');
 // Nothing animates for a terminal agent.
 assert.equal(frames.size,0);
 rooms.dispose();delete global.IntersectionObserver;d.window.close();
});
