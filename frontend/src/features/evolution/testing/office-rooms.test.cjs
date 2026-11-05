/** Executable specifications for the room directory (office-rooms.ts): six
 * always-visitable rooms, occupancy only from the swarm roster, and a member
 * card scoped to the selected department room. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const {JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 const full=path.resolve(path.dirname(parent),name+'.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/thumb.webp'}:name.endsWith('/i18n')?{t:k=>k}:load(name,full),exports);return exports;
}
function harness(){
 const d=new JSDOM('<nav id="nav"></nav><aside id="card"></aside>',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 const selected=[],opened=[];
 const rooms=load('office-rooms').mountOfficeRooms(document.getElementById('nav'),document.getElementById('card'),{copy:k=>k,select:id=>selected.push(id),openTask:id=>opened.push(id)});
 return {d,rooms,selected,opened,nav:document.getElementById('nav'),card:document.getElementById('card')};
}
const agents=[
 {id:'a1',role:'researcher',status:'running',objective:'Compare evaluation methods'},
 {id:'a2',role:'coder',status:'pending',objective:'Implement the prototype'},
 {id:'a3',role:'research analyst',status:'done',objective:'Summarize sources'},
];
test('six rooms stay discoverable without a roster; occupancy comes only from the swarm snapshot',()=>{
 const h=harness();h.rooms.update([],'',true);
 const tiles=[...h.nav.querySelectorAll('.evo-room-tile')];
 assert.equal(tiles.length,6);assert.deepEqual(tiles.map(t=>t.dataset.room),['','research','engineering','review','writing','general']);
 assert.equal(tiles[0].getAttribute('aria-pressed'),'true');assert.equal(tiles[0].getAttribute('aria-label'),'mainRoom');
 assert.ok(tiles.slice(1).every(t=>t.querySelector('.evo-room-count').hidden));
 h.rooms.update(agents,'task-1');
 assert.equal(tiles[1].querySelector('.evo-room-count').textContent,'2');assert.equal(tiles[1].querySelector('.evo-room-dot').dataset.status,'running');
 assert.equal(tiles[2].querySelector('.evo-room-dot').dataset.status,'pending');assert.equal(tiles[1].getAttribute('aria-label'),'roomResearch · 2');
 h.rooms.update(agents,'task-1',false);assert.equal(tiles[1].getAttribute('aria-label'),'roomResearch · ?');
 assert.equal(h.card.hidden,true,'the main office has no member card');
 h.rooms.dispose();h.d.window.close();
});
test('entering a department room selects it and scopes the card to that agent and task',()=>{
 const h=harness();h.rooms.update(agents,'task-1');
 h.nav.querySelector('[data-room=research]').click();
 assert.deepEqual(h.selected,['research']);assert.equal(h.rooms.active(),'research');assert.equal(h.card.hidden,false);
 assert.equal(h.card.querySelector('strong').textContent,'memberTitle_research','free-form roles read as the room theme job title');assert.equal(h.card.querySelector('.evo-chip').textContent,'running');
 assert.equal(h.card.querySelector('.evo-member-brief').textContent,'Compare evaluation methods');
 assert.equal(h.rooms.member().id,'a1');
 // Same-role members remain individually selectable.
 const tabs=[...h.card.querySelectorAll('.evo-member-tabs button')];assert.equal(tabs.length,2);
 assert.deepEqual(tabs.map(tab=>tab.textContent),['memberTitle_research 1','memberTitle_research 2']);
 tabs[1].click();assert.equal(h.rooms.member().id,'a3');assert.equal(h.card.querySelector('.evo-chip').textContent,'completed');
 h.card.querySelector('.evo-member-task').click();assert.deepEqual(h.opened,['task-1']);
 h.rooms.dispose();h.d.window.close();
});
test('a departed agent leaves an empty, still visitable room; an unknown roster stays unknown',()=>{
 const h=harness();h.rooms.update(agents,'task-1');h.rooms.select('engineering');
 h.rooms.update([],'task-1');
 assert.equal(h.rooms.member(),null);assert.equal(h.card.querySelector('strong').textContent,'roomEngineering');
 assert.equal(h.card.querySelector('.evo-chip').textContent,'roomUnoccupied');assert.equal(h.card.querySelector('.evo-member-task').hidden,true);
 h.rooms.update([],'task-1',false);assert.equal(h.card.querySelector('.evo-chip').textContent,'roomRosterUnknown');
 h.rooms.select('');assert.equal(h.card.hidden,true);
 h.rooms.dispose();h.d.window.close();
});
