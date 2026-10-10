/** Role-shaped actors and interactive workstation props over the office illustration. Presentation only.
 * Canvas 2D at <=30 FPS while walking; no RAF while stationary/hidden/offscreen.
 * Every pose change is driven by real run/coordinator/swarm state. Workstation
 * chairs move through pull/sit/roll-in and roll-out/stand/push beats. Each visible new agent-collected material is a real book the
 * executor picks up from its desk, carries across the floor and stacks on the
 * shelf — never a crop sinking into the floorboards. Actors also gather at the
 * front when the human is needed.
 * Idle actors remain still; visible-time beats pause offscreen without jumps.
 * DOM actor buttons preserve keyboard/task access. Owns and disposes all resources.
 * Sprite atlas: derived from generated art by scripts/generate_evolution_people.py.
 * Prop atlas: drawn deterministically by scripts/generate_evolution_props.py.
 */
import propsArt from './assets/props.png';
import chairArt from './assets/game-chair.webp';
import { esc } from './components/ui';
import { findOfficePath, type Point, FLOOR_WIDTH, FLOOR_HEIGHT } from './office-navigation';
import { createProp, drawProp, drawOfficeChair, placeProp, slideProp, stepProps, propSettled, PROP_SCALE, type Prop, type PropKind } from './office-objects';
import { modelForRole, type CharacterModel } from './office-character';
import { createCharacterPainter } from './office-character-art';

import { createOfficeCanvasSurface } from './office-canvas';
import { stepWalker, seatPose, SEAT_TRANSITION_MS, TURN_MS } from './office-motion';
export type OfficeRole = {name:string;state:string;taskId:string;working:boolean;waiting:boolean};
export type OfficeMaterial = {material_id:string;origin:'user'|'agent'};
type Walker = Point & {strideDistance:number;seatAmount:number;path:Point[];direction:number;frame:number;sprite:number;model:CharacterModel;seated:boolean;activity:string;button:HTMLButtonElement;onArrive?:()=>void};
// One scripted action per step; the sequencer below runs them in order.
type ChairAction = 'pull'|'roll-in'|'roll-out'|'push';
type Step = {kind:'walk';to:Point}|{kind:'sit'}|{kind:'stand'}|{kind:'chair';action:ChairAction}
  | {kind:'dwell';ms:number}|{kind:'pick';book:number}|{kind:'shelve';book:number}
  | {kind:'face';direction:number}|{kind:'activity';name:string};
type Actor = Walker & {index:number;role:OfficeRole;home:Point;station:Station;carrying:boolean;script:Step[];current?:Step;stepElapsed:number;poseFrom:number};
type Visitor = Walker & {id:string;role:string;status:string;leaving:boolean;desk:Point};
export type SwarmVisitor = {id:string;role:string;status:string};
type Station = Point & {face:number;seat:boolean};
// Directly in front of the top-center bookshelf (footprint [620,115,865,255]); within walkable bounds.
const SHELF_POINT:Point = {x:740,y:270};
const SHELF_DWELL_MS = 1600;
// An assignment is a committed handoff in the goal journal. The coordinator
// visits the executor's desk only for a new handoff, never for a poll refresh.
const HANDOFF_POINT:Point = {x:350,y:350};
// A tool event can put the executor at the real research shelf or beside the
// coordinator. A book appears only after the durable material list changes.
const CONSULT_POINT:Point = {x:230,y:350};
const DELEGATE_POINT:Point = {x:810,y:290};
function toolRoute(tool:string):{point:Point;activity:string}|undefined {
  const name=tool.toLowerCase();
  if(/^(web_search|fetch_url|browser_research_page|browser_search|browser_fetch|browser_read)/.test(name))
    return {point:SHELF_POINT,activity:'research'};
  if(name==='evolution_coordinate')return {point:CONSULT_POINT,activity:'talk'};
  if(name==='spawn_agents')return {point:DELEGATE_POINT,activity:'delegate'};
  return undefined;
}
const ROLE_NAMES = ['executor','reviewer','coordinator'];
// One unique sprite per role; swarm visitors cycle the three spare characters.
const SPRITES = [0,1,2];
const VISITOR_SPRITES = [3,4,5];
// Per-role workstations with a facing direction; desk stations own a pull-out
// chair (seated crop), the coordinator thinks standing at the plan board.
const STATIONS:Station[] = [{x:205,y:288,face:3,seat:true},{x:750,y:288,face:3,seat:true},{x:178,y:330,face:1,seat:false}];
const ACTIVITIES = ['working','reviewing','thinking'];
// Roles needing the human gather in front of the delivery table, one slot each.
// All cell centers verified walkable and connected to the floor.
const WAIT_POINTS:Point[] = [{x:440,y:590},{x:490,y:590},{x:530,y:590}];
// Sub-agents enter through the right-wall door and occupy hot desks while live.
// Matches the orchestration sub-agent ceiling; all points verified walkable.
const DOOR_POINT:Point = {x:895,y:190};
const VISITOR_DESKS:Point[] = [{x:480,y:470},{x:560,y:470},{x:400,y:470},{x:480,y:530},{x:560,y:530},{x:650,y:450},{x:300,y:360},{x:700,y:350}];
const MAX_VISITORS = 8;
const DOOR_DWELL_MS = 1400;
const CHAIR_IN = 10;
const CHAIR_OUT = 56;
const CHAIR_SLIDE_MS = 400;
// A newly found reference waits as a flat book on the executor desk edge...
const DESK_BOOK:Point = {x:208,y:252};
const DESK_FRONT:Point = {x:250,y:270};
// ...then gets stacked on the shelf's bottom board, two layers of four. The
// goal-room material list stays the authority; the stack display is capped.
const SHELF_STACK = (i:number):Point => ({x:676+(i%4)*20,y:249-Math.floor(i/4)*8});
const MAX_BOOKS = 8;
const chairInAt = (index:number):Point => ({x:STATIONS[index].x,y:STATIONS[index].y+CHAIR_IN});
const chairOutAt = (index:number):Point => ({x:STATIONS[index].x,y:STATIONS[index].y+CHAIR_OUT});
const chairSideAt = (index:number):Point => ({x:STATIONS[index].x+40,y:STATIONS[index].y+62});
const chairSeatOutAt = (index:number):Point => ({x:STATIONS[index].x,y:STATIONS[index].y+CHAIR_OUT-CHAIR_IN});
const bookKind = (prefix:string,slot:number) => `${prefix}${slot%4}` as PropKind;
export function mountOfficeScene(host: HTMLElement, controls: HTMLElement, options: {
  configureRole?:(index:number)=>void;copy:(key:string)=>string;openTask:(id:string)=>void;askHuman:()=>void;
}) {
  const {copy}=options;
  host.innerHTML=`<canvas aria-hidden="true"></canvas><div class="evo-door" data-open="false" aria-hidden="true"><i></i></div><div class="evo-actors"></div><span class="evo-walk-target" hidden></span>`;
  controls.innerHTML=`<span id="evo-walk-help">${esc(copy('walkHint'))}</span><button type="button" class="evo-scene-pause" aria-pressed="false">${esc(copy('pauseMotion'))}</button>`;
  const canvas=host.querySelector('canvas')!;
  const surface=createOfficeCanvasSurface(canvas);
  const ctx=surface.ctx;
  const pause=controls.querySelector('button')!;
  const target=host.querySelector<HTMLElement>('.evo-walk-target')!;
  const door=host.querySelector<HTMLElement>('.evo-door')!;
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const actors:Actor[]=[{x:430,y:355},{x:600,y:420},{x:480,y:250}].map((p,index)=>{
    const button=document.createElement('button');button.type='button';button.className='evo-person';
    button.dataset.actor=String(index);button.dataset.sprite=String(SPRITES[index]);button.setAttribute('aria-describedby','evo-walk-help');
    button.innerHTML='<span class="evo-person-label"></span><span class="evo-person-activity" aria-hidden="true"></span><span class="evo-person-bubble" hidden></span>';
    host.querySelector('.evo-actors')!.append(button);
    return {...p,index,home:p,station:STATIONS[index],strideDistance:0,seatAmount:0,path:[],direction:0,frame:1,sprite:SPRITES[index],model:modelForRole('',index),seated:false,activity:'',button,carrying:false,script:[],stepElapsed:0,poseFrom:0,role:{name:copy(ROLE_NAMES[index]),state:copy('idle'),taskId:'',working:false,waiting:false}};
  });
  const visitors = new Map<string,Visitor>();

  // Latest bounded live roster waits for departing avatars to free a desk.
  const desiredVisitors = new Map<string,SwarmVisitor>();
  // One chair per seated station and up to MAX_BOOKS material books: every
  // furniture part is an individual object with its own state and motion.
  const chairs:Prop[]=[0,1].map(index=>createProp(`chair-${index}`,'chair',chairInAt(index)));
  const chairButtons=chairs.map((_,index)=>{
    const button=document.createElement('button');button.type='button';button.className='evo-chair-interaction';
    button.dataset.chair=String(index);
    button.setAttribute('aria-label',`坐下或起身 · Sit or stand · ${actors[index].role.name}`);
    host.querySelector('.evo-actors')!.append(button);return button;
  });
  const books:Prop[]=[];
  const bookState:string[]=[]; // '' | 'desk' | 'carried' | 'shelf', indexed by material slot
  let carriedBook=-1;
  let disposed=false,paused=!!reduced?.matches,intersecting=true,raf=0,wake=0,last=0,drawAt=0,selected=0;
  let spriteHeight=76,tripping=false,doorWake=0,agentsTaskId='',goalId='',lastAssignmentSeq=0;
  let materialsInitialized=false;
  const materialIds:string[]=[];
  const knownMaterialIds=new Set<string>();
  const toolRoutes:string[]=['','',''];
  const propAtlas=new window.Image();
  const chairAtlas=new window.Image();
  const characterArt=createCharacterPainter(()=>{
    surface.invalidate();draw();
    if(characterArt.readyFor(actors.map(actor=>actor.model)))host.dataset.sprites='ready';
  });
  host.dataset.sprites='loading';
  function visible(){return !disposed&&!host.hidden&&!document.hidden&&intersecting;}
  function ensureArt(){
    if(!visible())return;
    walkers().forEach(walker=>characterArt.load(walker.model));
    if(!propAtlas.src)propAtlas.src=propsArt;
    if(!chairAtlas.src)chairAtlas.src=chairArt;
  }
  function walkers():Walker[]{return [...actors,...visitors.values()];}
  function propsMoving(){return chairs.some(prop=>!propSettled(prop))||books.some(prop=>!propSettled(prop));}
  // Visible-time action clock: hidden or paused scenes never complete beats
  // in a background timer and then jump on re-entry.
  function timedStepPending(){return actors.some(actor=>['dwell','face','sit','stand'].includes(actor.current?.kind||''));}
  function moving(){return walkers().some(w=>w.path.length>0)||propsMoving()||timedStepPending();}
  function positionButton(walker:Walker){
    walker.button.style.left=`${walker.x/FLOOR_WIDTH*100}%`;
    walker.button.style.top=`${walker.y/FLOOR_HEIGHT*100}%`;
    walker.button.style.height=`${spriteHeight/FLOOR_HEIGHT*100}%`;
    walker.button.dataset.phase=walker.path.length?'walk':(walker as Actor).current?.kind|| (walker.seated?'seated':'idle');
    walker.button.dataset.frame=String(walker.frame);
    walker.button.dataset.direction=String(walker.direction);
    walker.button.dataset.seatAmount=walker.seatAmount.toFixed(3);
  }
  function syncButton(actor:Actor,index:number){
    positionButton(actor);
    actor.button.dataset.selected=String(index===selected);
    actor.button.dataset.working=String(actor.role.working);
    actor.button.dataset.waiting=String(actor.role.waiting);
    actor.button.dataset.carrying=String(actor.carrying);
    actor.button.dataset.seated=String(actor.seated);
    actor.button.dataset.activity=actor.activity;
    actor.button.dataset.model=actor.model;
    actor.button.setAttribute('aria-pressed',String(index===selected));
    actor.button.setAttribute('aria-label',`${actor.role.name} · ${actor.role.state}`);
    actor.button.title=`${actor.role.name} · ${actor.role.state}${actor.role.taskId?' · '+copy('inspectTask'):''}`;
    actor.button.firstElementChild!.textContent=actor.role.name;
  }
  function syncVisitor(visitor:Visitor){
    positionButton(visitor);
    visitor.button.dataset.status=visitor.status;
    visitor.button.dataset.activity=visitor.status==='running'?'working':'';
    visitor.button.dataset.model=visitor.model;
    visitor.button.setAttribute('aria-label',`${visitor.role} · ${visitor.status}`);
    visitor.button.title=`${visitor.role} · ${visitor.status}`;
    visitor.button.firstElementChild!.textContent=visitor.role;
  }
  const buttonSignatures=new WeakMap<Walker,string>();
  function draw(){
    if(!visible())return;
    ensureArt();
    walkers().forEach(walker=>{
      const actor=walker as Actor,visitor=walker as Visitor;
      const signature=JSON.stringify([walker.x,walker.y,spriteHeight,walker.frame,walker.direction,walker.seatAmount,walker.seated,walker.activity,actor.current?.kind,actor.carrying,actor.role,visitor.status,walker.model,selected]);
      if(buttonSignatures.get(walker)===signature)return;
      buttonSignatures.set(walker,signature);
      if('index' in walker)syncButton(actor,actor.index);else syncVisitor(visitor);
    });
    // Presentation-state mirrors for tests and diagnostics; the props stay the authority.
    host.dataset.chairs=chairs.map((chair,index)=>propSettled(chair)?
      (chair.y>chairInAt(index).y+1?'out':'in'):'moving').join(',');
    host.dataset.chairActions=actors.slice(0,2).map(actor=>actor.current?.kind==='chair'?actor.current.action:'').join(',');
    chairs.forEach((chair,index)=>{
      const button=chairButtons[index];button.style.left=`${(chair.x+22)/FLOOR_WIDTH*100}%`;
      button.style.top=`${(chair.y-7)/FLOOR_HEIGHT*100}%`;
      button.setAttribute('aria-label',`坐下或起身 · Sit or stand · ${actors[index].role.name}`);
    });
    host.dataset.books=bookState.join(',');
    host.dataset.materialIds=materialIds.join(',');
    if(!ctx)return;
    const signature=JSON.stringify([spriteHeight,selected,walkers().map(w=>[w.x,w.y,w.frame,w.direction,w.seatAmount,w.model,(w as Actor).carrying]),chairs.map(p=>[p.x,p.y]),books.map(p=>[p.x,p.y,p.kind,p.hidden]),bookState]);
    surface.paint(signature,()=>{
    // Desk and shelf books always sit behind the walkers (actors never cross them).
    books.forEach((book,index)=>{if(bookState[index]&&bookState[index]!=='carried')drawProp(ctx,propAtlas,book,spriteHeight*PROP_SCALE.bookFlat);});
    // Rear-facing chair backs occlude the lowered sitter. Sorting by the
    // unchanged foot anchor avoids a depth pop at the sit/stand boundary.
    const drawables:Array<Walker|Prop>=[...chairs,...walkers()];
    const depth=(item:Walker|Prop)=>item.y;
    drawables.sort((a,b)=>depth(a)-depth(b)).forEach(item=>{
      if('from' in item){
        drawOfficeChair(ctx,chairAtlas,item.x,item.y,spriteHeight*.72);return;
      }
      const walker=item;
      ctx.fillStyle='rgba(42,32,21,.23)';ctx.beginPath();ctx.ellipse(walker.x,walker.y-2,19-5*walker.seatAmount,6,0,0,Math.PI*2);ctx.fill();
      if(actors[selected]===walker){ctx.strokeStyle='#f7edc3';ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(walker.x,walker.y-2,23,8,0,0,Math.PI*2);ctx.stroke();}
      characterArt.draw(ctx,walker.model,walker.sprite,walker.direction,walker.frame,
        walker.x,walker.y,spriteHeight,walker.seatAmount);
      // A carried book rides under one arm as a real object, not a CSS badge.
      if((walker as Actor).carrying&&carriedBook>=0&&books[carriedBook]){
        const book=books[carriedBook],side=walker.direction===1?-1:1;
        book.x=walker.x+13*side;book.y=walker.y-spriteHeight*.42;
        drawProp(ctx,propAtlas,book,spriteHeight*PROP_SCALE.bookUp);
      }
    });
    });
  }
  function resize(){
    if(disposed)return;
    const width=host.clientWidth||640;
    surface.resize(width,window.devicePixelRatio||1);
    spriteHeight=Math.min(108,Math.max(76,34*FLOOR_WIDTH/width));draw();
  }
  function stop(){window.cancelAnimationFrame(raf);window.clearTimeout(wake);window.clearTimeout(doorWake);doorWake=0;door.dataset.open='false';raf=0;wake=0;last=0;drawAt=0;}
  function requestFrame(){if(!raf&&visible()&&!paused&&moving())raf=window.requestAnimationFrame(tick);}
  function tick(now:number){
    raf=0;if(!visible()||paused)return;
    const frameMs=1000/30;
    const slots=Math.floor((now-drawAt+1e-6)/frameMs);
    if(slots<1){requestFrame();return;}
    const dt=Math.min(last?(now-last)/1000:1/30,.06);last=now;
    // Keep the fractional display-frame remainder (e.g. 144 Hz); assigning
    // drawAt=now drifts below 30 Hz and adds uneven gait/seat frame spacing.
    drawAt+=slots*frameMs;
    // Snapshot before *any* arrivals, including chair rolls. A new beat
    // starts at t=0 instead of reusing time spent finishing the prior beat.
    const beats=actors.map(actor=>actor.current);
    stepProps(chairs,dt*1000);stepProps(books,dt*1000);
    actors.slice(0,2).forEach(actor=>{
      if(actor.current?.kind==='chair'&&actor.current.action.startsWith('roll-')){
        actor.x=chairs[actor.index].x;actor.y=chairs[actor.index].y-CHAIR_IN;
        if(propSettled(chairs[actor.index]))finishStep(actor);
      }else if(actor.current?.kind==='chair'&&propSettled(chairs[actor.index]))finishStep(actor);
    });
    walkers().forEach(walker=>{
      if('index' in walker&&(walker as Actor).current!==beats[(walker as Actor).index])return;
      if(stepWalker(walker,dt*1000)){
        const arrive=walker.onArrive;walker.onArrive=undefined;arrive?.();
      }
    });
    actors.forEach((actor,index)=>{
      const step=actor.current;
      if(!step||step!==beats[index])return;
      if(step.kind==='sit'||step.kind==='stand'){
        actor.stepElapsed+=dt*1000;
        actor.seatAmount=seatPose(actor.poseFrom,step.kind==='sit'?1:0,actor.stepElapsed);
        if(actor.stepElapsed>=SEAT_TRANSITION_MS){actor.seated=step.kind==='sit';finishStep(actor);}
      }else if(step.kind==='dwell'||step.kind==='face'){
        actor.stepElapsed+=dt*1000;
        if(actor.stepElapsed>=(step.kind==='face'?TURN_MS:step.ms))finishStep(actor);
      }
    });
    draw();if(moving())requestFrame();else{last=0;target.hidden=true;idle();}
  }
  // Reduced motion resolves an explicit intent once, without a fake walking
  // loop. Unreachable destinations never run the next sit/pick continuation.
  function send(walker:Walker,point:Point,arrive?:()=>void){
    const path=findOfficePath(walker,point);

    if(!path.length&&Math.hypot(walker.x-point.x,walker.y-point.y)>.001){
      if('index' in walker)cancelScript(walker as Actor);
      return;
    }
    walker.activity='';walker.onArrive=arrive;
    if(paused){const end=path.at(-1);if(end){walker.x=end.x;walker.y=end.y;}walker.path=[];walker.frame=1;}
    else walker.path=path;
    if(!walker.path.length){const done=walker.onArrive;walker.onArrive=undefined;done?.();}
  }
  // One action per actor; the visible-time scene tick is the sole clock.
  function finishStep(actor:Actor){
    actor.current=undefined;actor.stepElapsed=0;
    advance(actor);
  }
  function advance(actor:Actor){
    const step=actor.script.shift();
    // A finished executor script also releases the trip latch, so a book
    // waiting on the desk starts its shelving run right away.
    if(!step){draw();if(actor.index===0){tripping=false;beginTrip();}return;}
    actor.current=step;
    actor.stepElapsed=0;
    if(step.kind==='walk'){send(actor,step.to,()=>finishStep(actor));requestFrame();return;}
    if(step.kind==='dwell'){
      if(paused){finishStep(actor);return;}
      requestFrame();return;
    }
    if(step.kind==='chair'){
      const chair=chairs[actor.index];
      const destination=step.action==='pull'||step.action==='roll-out'?chairOutAt(actor.index):chairInAt(actor.index);
      slideProp(chair,destination,paused?0:CHAIR_SLIDE_MS);
      if(paused){if(step.action.startsWith('roll-')){actor.x=chair.x;actor.y=chair.y-CHAIR_IN;}finishStep(actor);return;}
      requestFrame();return;
    }
    if(step.kind==='sit'||step.kind==='stand'){
      actor.poseFrom=actor.seatAmount;actor.frame=1;
      if(paused){actor.seated=step.kind==='sit';actor.seatAmount=actor.seated?1:0;finishStep(actor);}
      else requestFrame();
      return;
    }
    if(step.kind==='face'){
      const changed=actor.direction!==step.direction;
      actor.direction=step.direction;actor.frame=1;
      if(paused||!changed)finishStep(actor);else requestFrame();
      return;
    }
    if(step.kind==='activity'){actor.activity=step.name;finishStep(actor);return;}
    if(step.kind==='pick'){pickBook(step.book,actor);finishStep(actor);return;}
    if(step.kind==='shelve'){shelveBook(step.book,actor);finishStep(actor);return;}
  }
  function runScript(actor:Actor,steps:Step[]){cancelScript(actor);actor.script=steps;advance(actor);}
  function cancelScript(actor:Actor){
    actor.script=[];actor.current=undefined;actor.stepElapsed=0;
    actor.path=[];actor.onArrive=undefined;actor.frame=1;
    if(actor.station.seat){
      const chair=chairs[actor.index];
      // Keep a seated actor attached to the actual interrupted position.
      // Empty chairs return smoothly instead of resetting under the actor.
      if(actor.seated||actor.seatAmount>0)placeProp(chair,chair);
      else slideProp(chair,chairInAt(actor.index),paused?0:CHAIR_SLIDE_MS);
    }
  }
  // A desk has a physical chair. Sit and stand scripts keep the avatar attached
  // while the chair rolls, then tuck the empty chair back under the desk.
  function standSteps(actor:Actor):Step[]{return actor.seated||actor.seatAmount>0?[
    {kind:'chair',action:'roll-out'},{kind:'stand'},
    {kind:'walk',to:chairSideAt(actor.index)},{kind:'chair',action:'push'}]:[];}
  function sitSteps(actor:Actor,activity:string,afterLeaving=false):Step[]{
    if(!actor.station.seat)return[{kind:'walk',to:actor.station},{kind:'face',direction:actor.station.face},{kind:'activity',name:activity}];
    if((actor.seated||actor.seatAmount>0)&&!afterLeaving)return[
      {kind:'face',direction:actor.station.face},{kind:'sit'},{kind:'chair',action:'roll-in'},{kind:'activity',name:activity}];
    return [{kind:'walk',to:chairSideAt(actor.index)},
      {kind:'chair',action:'pull'},{kind:'walk',to:chairSeatOutAt(actor.index)},
      {kind:'face',direction:actor.station.face},{kind:'sit'},{kind:'chair',action:'roll-in'},{kind:'activity',name:activity}];
  }
  // Book lifecycle: desk (new material, waiting) → carried → shelf. The real
  // agent-material count owns spawns/removals; these are only the visuals.
  function restackDesk(){
    let n=0;
    bookState.forEach((state,slot)=>{if(state==='desk')placeProp(books[slot],{x:DESK_BOOK.x,y:DESK_BOOK.y-n++*5});});
  }
  function pickBook(slot:number,actor:Actor){
    if(bookState[slot]!=='desk')return;
    bookState[slot]='carried';carriedBook=slot;
    books[slot].kind=bookKind('bookUp',slot);
    actor.carrying=true;restackDesk();
  }
  function shelveBook(slot:number,actor:Actor){
    if(bookState[slot]!=='carried')return;
    actor.carrying=false;carriedBook=-1;
    bookState[slot]='shelf';books[slot].kind=bookKind('bookFlat',slot);
    placeProp(books[slot],SHELF_STACK(slot));
  }
  function reconcileBooks(materials:OfficeMaterial[]){
    const visibleMaterials=materials.slice(0,MAX_BOOKS);
    const nextIds=visibleMaterials.map(item=>item.material_id);
    if(nextIds.join('\0')===materialIds.join('\0'))return;
    // A goal can change while a trip is in flight. Rebuild from durable link
    // identities so deleting the middle item never assigns its book to another.
    if(tripping||carriedBook>=0)endTrip();
    const previous=new Map(materialIds.map((id,index)=>[id,{book:books[index],state:bookState[index]}]));
    books.length=0;bookState.length=0;materialIds.length=0;
    visibleMaterials.forEach((item,slot)=>{
      const old=previous.get(item.material_id);
      const discovered=materialsInitialized&&!knownMaterialIds.has(item.material_id);
      const state=old?.state==='desk'?'desk':discovered&&item.origin==='agent'&&actors[0].role.working?'desk':'shelf';
      const book=old?.book||createProp(`book-${item.material_id}`,bookKind('bookFlat',slot),DESK_BOOK);
      book.kind=bookKind('bookFlat',slot);book.hidden=false;
      books.push(book);bookState.push(state);materialIds.push(item.material_id);
      if(state==='shelf')placeProp(book,SHELF_STACK(slot));
    });
    materials.forEach(item=>knownMaterialIds.add(item.material_id));
    materialsInitialized=true;restackDesk();
  }
  // Executor material shelving, triggered by the real agent-material count
  // growing: stand → desk → pick the waiting book → carry → stack it on the
  // shelf → sit back down to work. The one visible-time frame clock owns
  // continuations; hidden trips pause at the current pose and resume there.
  function endTrip(){
    tripping=false;
    const actor=actors[0];
    cancelScript(actor);
    if(carriedBook>=0){
      bookState[carriedBook]='desk';books[carriedBook].kind=bookKind('bookFlat',carriedBook);
      placeProp(books[carriedBook],DESK_BOOK);restackDesk();carriedBook=-1;
    }
    actor.carrying=false;
  }
  function beginTrip(){
    const actor=actors[0];
    if(tripping||actor.carrying||actor.script.length||actor.current||actor.path.length||!actor.role.working)return;
    const book=bookState.findIndex(state=>state==='desk');
    if(book<0||!findOfficePath(actor,SHELF_POINT).length)return;
    tripping=true;
    runScript(actor,[...standSteps(actor),
      {kind:'walk',to:DESK_FRONT},{kind:'face',direction:3},{kind:'pick',book},
      {kind:'walk',to:SHELF_POINT},{kind:'face',direction:3},{kind:'activity',name:'fetch'},
      {kind:'dwell',ms:SHELF_DWELL_MS},{kind:'shelve',book},{kind:'activity',name:''},
      ...sitSteps(actor,ACTIVITIES[0],true)]);
  }
  // Sub-agent visitors: enter through the door while the swarm roster lists
  // them live, walk back out and vanish once terminal or dropped. One bounded
  // door timer; visitors never patrol and keep the DOM walk targets of actors.
  function openDoor(){
    if(!visible()||paused)return;
    door.dataset.open='true';window.clearTimeout(doorWake);
    doorWake=window.setTimeout(()=>{doorWake=0;door.dataset.open='false';},DOOR_DWELL_MS);
  }
  function spawnVisitor(agent:SwarmVisitor){
    const used=new Set([...visitors.values()].map(visitor=>visitor.desk));
    const desk=VISITOR_DESKS.find(point=>!used.has(point))||VISITOR_DESKS[0];
    const button=document.createElement('button');button.type='button';button.className='evo-person evo-visitor';
    button.dataset.visitor=agent.id;button.setAttribute('aria-describedby','evo-walk-help');
    button.innerHTML='<span class="evo-person-label"></span><span class="evo-person-activity" aria-hidden="true"></span>';
    const sprite=VISITOR_SPRITES[visitors.size%VISITOR_SPRITES.length];
    button.dataset.sprite=String(sprite);
    host.querySelector('.evo-actors')!.append(button);
    const visitor:Visitor={...DOOR_POINT,id:agent.id,role:(agent.role||'agent').slice(0,24),status:agent.status,leaving:false,desk,
      strideDistance:0,seatAmount:0,path:[],direction:1,frame:1,sprite,model:modelForRole(agent.role),seated:false,activity:'',button};
    visitors.set(agent.id,visitor);openDoor();
    if(paused){visitor.x=desk.x;visitor.y=desk.y;}
    else visitor.path=findOfficePath(visitor,desk);
  }
  function admitVisitors(){
    if(disposed)return;
    for(const agent of desiredVisitors.values()){
      if(visitors.size>=MAX_VISITORS)break;
      if(!visitors.has(agent.id))spawnVisitor(agent);
    }
  }
  function dismissVisitor(visitor:Visitor){
    if(visitor.leaving)return;
    visitor.leaving=true;
    const vanish=()=>{visitor.button.remove();visitors.delete(visitor.id);openDoor();admitVisitors();};
    const path=findOfficePath(visitor,DOOR_POINT);
    if(paused||!path.length){visitor.path=[];vanish();return;}
    visitor.onArrive=vanish;visitor.path=path;
  }
  function reconcileVisitors(list:SwarmVisitor[],taskId:string){
    agentsTaskId=taskId;
    desiredVisitors.clear();
    list.filter(agent=>agent.status==='running'||agent.status==='pending').slice(0,MAX_VISITORS)
      .forEach(agent=>desiredVisitors.set(agent.id,agent));
    // Snapshot: a reduced-motion departure may synchronously admit a new visitor.
    [...visitors.values()].forEach(visitor=>{
      const agent=desiredVisitors.get(visitor.id);
      if(!agent){dismissVisitor(visitor);return;}
      visitor.status=agent.status;visitor.role=(agent.role||'agent').slice(0,24);visitor.model=modelForRole(agent.role);
      if(visitor.leaving){
        visitor.leaving=false;visitor.onArrive=undefined;send(visitor,visitor.desk);
      }
    });
    admitVisitors();
  }
  // Stationary means zero callbacks. Idle actors do not patrol or cycle gait
  // frames as a surrogate for backend progress.
  function idle(){window.clearTimeout(wake);wake=0;}
  function settlePaused(){
    if(!paused)return;
    actors.forEach(actor=>{
      const step=actor.current;
      if(!step)return;
      if(step.kind==='walk'){
        const end=actor.path.at(-1);if(end){actor.x=end.x;actor.y=end.y;}
        actor.path=[];actor.onArrive=undefined;actor.frame=1;
      }else if(step.kind==='chair'){
        const chair=chairs[actor.index];placeProp(chair,chair.to);
        if(step.action.startsWith('roll-')){actor.x=chair.x;actor.y=chair.y-CHAIR_IN;}
      }else if(step.kind==='sit'||step.kind==='stand'){
        actor.seated=step.kind==='sit';actor.seatAmount=actor.seated?1:0;
      }
      finishStep(actor);
    });
    visitors.forEach(visitor=>{
      const end=visitor.path.at(-1);if(end){visitor.x=end.x;visitor.y=end.y;}
      visitor.path=[];visitor.frame=1;
      const arrive=visitor.onArrive;visitor.onArrive=undefined;arrive?.();
    });
    chairs.forEach(chair=>placeProp(chair,chair.to));
  }
  function settleMotion(){stop();settlePaused();pause.textContent=copy(paused?'resumeMotion':'pauseMotion');pause.setAttribute('aria-pressed',String(paused));host.dataset.paused=String(paused);draw();requestFrame();if(!moving())idle();}
  function toggle(){paused=!paused;settleMotion();}
  function preference(){paused=!!reduced?.matches;settleMotion();}
  function visibility(){if(visible()){draw();requestFrame();if(!moving())idle();}else stop();}
  function walk(point:Point){
    const actor=actors[selected],path=findOfficePath(actor,point);if(!path.length)return;
    window.clearTimeout(wake);wake=0;
    if(actor===actors[0])endTrip();else cancelScript(actor);
    actor.onArrive=undefined;actor.carrying=false;
    actor.activity='';
    target.hidden=paused;target.style.left=`${point.x/FLOOR_WIDTH*100}%`;target.style.top=`${point.y/FLOOR_HEIGHT*100}%`;
    runScript(actor,[...standSteps(actor),{kind:'walk',to:point}]);requestFrame();
  }
  function click(event:MouseEvent){
    const chairButton=(event.target as Element).closest<HTMLButtonElement>('.evo-chair-interaction');
    if(chairButton){
      const index=Number(chairButton.dataset.chair),actor=actors[index];selected=index;
      if(index===0)endTrip();
      runScript(actor,actor.seated?[...standSteps(actor),{kind:'activity',name:''}]:sitSteps(actor,actor.role.working?ACTIVITIES[index]:''));
      draw();requestFrame();return;
    }
    const button=(event.target as Element).closest<HTMLButtonElement>('.evo-person');
    if(button){
      if(button.dataset.visitor!==undefined){if(agentsTaskId)options.openTask(agentsTaskId);return;}
      selected=Number(button.dataset.actor);draw();if(options.configureRole){options.configureRole(selected);return;}const role=actors[selected].role;if(role.waiting)options.askHuman();else if(role.taskId)options.openTask(role.taskId);return;}
    const rect=host.getBoundingClientRect();walk({x:(event.clientX-rect.left)/rect.width*FLOOR_WIDTH,y:(event.clientY-rect.top)/rect.height*FLOOR_HEIGHT});
  }
  function keydown(event:KeyboardEvent){
    const button=(event.target as Element).closest<HTMLButtonElement>('.evo-person');if(!button||button.dataset.actor===undefined)return;
    const delta:Record<string,Point>={ArrowLeft:{x:-40,y:0},ArrowRight:{x:40,y:0},ArrowUp:{x:0,y:-40},ArrowDown:{x:0,y:40}};
    const d=delta[event.key];if(!d)return;event.preventDefault();selected=Number(button.dataset.actor);const a=actors[selected];walk({x:a.x+d.x,y:a.y+d.y});
  }
  const resizeObserver=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):undefined;
  resizeObserver?.observe(host);
  const observer=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{intersecting=entries[0]?.isIntersecting??false;visibility();}):undefined;
  observer?.observe(host);
  host.addEventListener('click',click);host.addEventListener('keydown',keydown);pause.addEventListener('click',toggle);
  document.addEventListener('visibilitychange',visibility);reduced?.addEventListener('change',preference);
  propAtlas.onload=()=>{if(disposed)return;surface.invalidate();draw();host.dataset.props='ready';};propAtlas.onerror=()=>{if(!disposed)host.dataset.props='unavailable';};
  chairAtlas.onload=()=>{if(!disposed){surface.invalidate();draw();}};
  resize();settleMotion();
  return {
    update(roles:OfficeRole[],state:{goalId?:string;materials?:OfficeMaterial[];assignmentSeq?:number;agents?:SwarmVisitor[];agentsTaskId?:string}={}){
      if(disposed)return;
      if((state.goalId||'')!==goalId){
        endTrip();goalId=state.goalId||'';lastAssignmentSeq=state.assignmentSeq||0;
        toolRoutes.fill('');
        books.length=0;bookState.length=0;materialIds.length=0;knownMaterialIds.clear();materialsInitialized=false;
        host.dataset.assignmentSeq=String(lastAssignmentSeq);
      }
      roles.slice(0,actors.length).forEach((role,index)=>{
        const actor=actors[index],changed=role.working!==actor.role.working||role.waiting!==actor.role.waiting||role.taskId!==actor.role.taskId;
        actor.role=role;
        if(changed){
          toolRoutes[index]='';
          if(index===0)endTrip();else cancelScript(actor);
          actor.carrying=false;
          if(role.waiting)runScript(actor,[...standSteps(actor),{kind:'walk',to:WAIT_POINTS[index]},{kind:'face',direction:0},{kind:'activity',name:'waiting'}]);
          else if(role.working)runScript(actor,sitSteps(actor,ACTIVITIES[index]));
          else runScript(actor,[...standSteps(actor),{kind:'walk',to:actor.home},{kind:'activity',name:''}]);
        }
      });
      if(state.materials)reconcileBooks(state.materials);
      // A completed task can leave a book mid-trip; the persisted shelf is
      // already authoritative, so settle that pending visual on the shelf.
      if(!actors[0].role.working)bookState.forEach((book,slot)=>{
        if(book==='desk'){bookState[slot]='shelf';placeProp(books[slot],SHELF_STACK(slot));}
      });
      if((state.assignmentSeq||0)>lastAssignmentSeq){
        lastAssignmentSeq=state.assignmentSeq||0;
        const coordinator=actors[2];
        runScript(coordinator,[...standSteps(coordinator),{kind:'walk',to:HANDOFF_POINT},
          {kind:'face',direction:1},{kind:'activity',name:'talk'},{kind:'dwell',ms:1100},
          {kind:'walk',to:coordinator.station},{kind:'face',direction:coordinator.station.face},
          {kind:'activity',name:coordinator.role.working?'thinking':''}]);
        host.dataset.assignmentSeq=String(lastAssignmentSeq);
      }
      reconcileVisitors(state.agents||[],state.agentsTaskId||'');
      beginTrip();
      draw();requestFrame();if(!moving())idle();
    },
    /** Live tool bubble above one actor, fed by the task SSE stream. The
     * label lives inside the actor button, so it follows every walk for
     * free; presentation stays decoration over the polling authority. */
    setToolBubble(index:number,label:string,tool=''){
      if(disposed)return;
      const actor=actors[index];
      const bubble=actor?.button.querySelector<HTMLElement>('.evo-person-bubble');
      if(!bubble)return;
      bubble.textContent=label;
      bubble.hidden=!label;
      const route=label?toolRoute(tool):undefined;
      bubble.dataset.kind=route?.activity||'';
      if(route&&actor.role.working&&!tripping&&toolRoutes[index]!==tool){
        toolRoutes[index]=tool;
        runScript(actor,[...standSteps(actor),{kind:'walk',to:route.point},
          {kind:'activity',name:route.activity}]);
        requestFrame();
        return;
      }
      if(toolRoutes[index]&&toolRoutes[index]!==tool){
        toolRoutes[index]='';
        if(actor.role.working&&!tripping)runScript(actor,sitSteps(actor,ACTIVITIES[index]));
        requestFrame();draw();return;
      }
    },
    metrics(){return {...surface.metrics(),pendingFrames:Number(!!raf),actors:actors.length+visitors.size};},
    dispose(){if(disposed)return;disposed=true;stop();actors.forEach(cancelScript);window.clearTimeout(doorWake);visitors.forEach(visitor=>visitor.button.remove());visitors.clear();desiredVisitors.clear();characterArt.dispose();propAtlas.onload=null;propAtlas.onerror=null;chairAtlas.onload=null;resizeObserver?.disconnect();observer?.disconnect();document.removeEventListener('visibilitychange',visibility);reduced?.removeEventListener('change',preference);host.removeEventListener('click',click);host.removeEventListener('keydown',keydown);pause.removeEventListener('click',toggle);surface.dispose();},
  };
}
