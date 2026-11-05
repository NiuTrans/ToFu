/** Main office scene for the game stage. Projects the studio view onto the
 * three resident roles, swarm visitors, material books, lit screens and
 * furniture hotspots over assets/office-clear.webp, plus the human's own
 * avatar. Resident poses come only from real run/coordinator/swarm state;
 * the player avatar is the one character the human steers.
 * Owns the per-actor action sequencer (walk, chair pull/sit/roll, stand, dwell,
 * pick/shelve), visible-time beats, hit testing, accessible targets and
 * teardown; the stage owns loop, camera and input. Entry: createOfficeScene.
 */
import officeArt from './assets/office-clear.webp';
import propsArt from './assets/props.png';
import chairArt from './assets/game-chair.webp';
import { findOfficePath, walkable, type Point } from './office-navigation';
import { createProp, drawProp, drawOfficeChair, placeProp, slideProp, stepProps, propSettled, PROP_SCALE, type Prop, type PropKind } from './office-objects';
import { modelForRole, type CharacterModel } from './office-character';
import { roleTitleKey } from './room-catalog';
import { createCharacterPainter } from './office-character-art';
import { stepWalker, seatPose, facing, SEAT_TRANSITION_MS, TURN_MS, STRIDE_DISTANCE, WALK_FRAMES } from './office-motion';
import { emoteFrameMs, makeBackdrop, PALETTE } from './engine/paint';
import { createParticles } from './engine/particles';
import type { Scene, SceneFrame, StageTarget } from './engine/stage';
import type { ActorMode, ActorRole, Emote, StudioView } from './studio/projection';
import { WORLD, PLACES, HOTSPOTS, STATIONS, HOMES, WAIT_POINTS, SHELF_POINT, DESK_BOOK, DESK_FRONT, HANDOFF_POINT,
  CONSULT_POINT, DELEGATE_POINT, DOOR_POINT, OUTSIDE_POINT, PLAYER_START, SHELF_STACK, MAX_BOOKS, VISITOR_DESKS,
  MAX_VISITORS, MONITORS, DELIVERY_DOCUMENT, SPRITE_HEIGHT, INTERACT_RANGE, type Place, type Station } from './office-layout';

export type OfficeTarget = {kind:'place';place:Place}|{kind:'actor';role:ActorRole}|{kind:'visitor';id:string};
export type OfficeMaterial = {material_id:string;origin:'user'|'agent'};
export type SwarmVisitor = {id:string;role:string;status:string};
export interface OfficeState {
  goalId:string;
  view:StudioView;
  materials:OfficeMaterial[];
  assignmentSeq:number;
  agents:SwarmVisitor[];
  names:Record<ActorRole,string>;
  actorLabels:Record<ActorRole,string>;
  placeLabels:Record<Place,string>;
  attention:Place[];
}
type Walker = Point & {path:Point[];direction:number;frame:number;strideDistance:number;seatAmount:number;
  seated:boolean;model:CharacterModel;sprite:number;activity:string;onArrive?:()=>void;speed:number};
type ChairAction = 'pull'|'roll-in'|'roll-out'|'push';
type Step = {kind:'walk';to:Point}|{kind:'sit'}|{kind:'stand'}|{kind:'chair';action:ChairAction}
  |{kind:'dwell';ms:number}|{kind:'pick';book:number}|{kind:'shelve';book:number}
  |{kind:'face';direction:number}|{kind:'activity';name:string};
type Actor = Walker & {role:ActorRole;home:Point;station:Station;mode:ActorMode;posture:string;emote:Emote;taskId:string;
  carrying:boolean;script:Step[];current?:Step;stepElapsed:number;poseFrom:number;tool:string;toolRoute:string};
type Visitor = Walker & {id:string;role:string;status:string;leaving:boolean;desk:Point};
type Player = Walker & {target?:Point;targetAge:number;steered:boolean};

const ROLES:ActorRole[] = ['executor','reviewer','coordinator'];
const ACTIVITIES:Record<ActorRole,string> = {executor:'working',reviewer:'reviewing',coordinator:'thinking'};
const WALK_SPEED = 95, PLAYER_SPEED = 165;
const CHAIR_IN = 10, CHAIR_OUT = 56, CHAIR_SLIDE_MS = 400;
const SHELF_DWELL_MS = 1600, HANDOFF_DWELL_MS = 1100;
/** Two-state ambient frames for lit screens and the waiting deliverable. */
const SCREEN_FRAME_MS = 640, DELIVERY_FRAME_MS = 700, ATTENTION_FRAME_MS = 600;
const bookKind = (prefix:string,slot:number) => `${prefix}${slot%4}` as PropKind;
function toolRoute(tool:string):{point:Point;face:number;activity:string}|undefined {
  const name=tool.toLowerCase();
  if(/^(web_search|fetch_url|browser_research_page|browser_search|browser_fetch|browser_read)/.test(name))return {point:SHELF_POINT,face:3,activity:'research'};
  if(name==='evolution_coordinate')return {point:CONSULT_POINT,face:1,activity:'talk'};
  if(name==='spawn_agents')return {point:DELEGATE_POINT,face:0,activity:'delegate'};
  return undefined;
}
function walker(at:Point,model:CharacterModel,sprite:number,speed=WALK_SPEED):Walker {
  return {x:at.x,y:at.y,path:[],direction:0,frame:1,strideDistance:0,seatAmount:0,seated:false,model,sprite,activity:'',speed};
}

export function createOfficeScene(ports:{activate:(target:OfficeTarget)=>void;wake:()=>void;copy:(key:string)=>string}) {
  const art=new window.Image(),propAtlas=new window.Image(),chairAtlas=new window.Image();
  let backdrop:HTMLCanvasElement|undefined,disposed=false,still=false;
  const characters=createCharacterPainter(()=>{if(!disposed)ports.wake();});
  const particles=createParticles();
  const actors:Record<ActorRole,Actor> = Object.fromEntries(ROLES.map(role=>[role,{
    ...walker(HOMES[role],modelForRole('',ROLES.indexOf(role)),ROLES.indexOf(role)),role,home:HOMES[role],station:STATIONS[role],
    mode:'idle' as ActorMode,posture:'idle',emote:'' as Emote,taskId:'',carrying:false,script:[],stepElapsed:0,poseFrom:0,tool:'',toolRoute:'',
  }])) as unknown as Record<ActorRole,Actor>;
  const player:Player={...walker(PLAYER_START,'writer',5,PLAYER_SPEED),direction:3,targetAge:0,steered:false};
  const visitors=new Map<string,Visitor>(),desired=new Map<string,SwarmVisitor>();
  const seated:ActorRole[]=['executor','reviewer'];
  const chairs:Record<string,Prop>=Object.fromEntries(seated.map(role=>[role,createProp(`chair-${role}`,'chair',chairInAt(role))]));
  const books:Prop[]=[],bookState:string[]=[],materialIds:string[]=[],knownMaterials=new Set<string>();
  let carriedBook=-1,tripping=false,materialsReady=false,goalId='',assignmentSeq=0,selected:ActorRole|''='';
  let state:OfficeState|undefined;
  let view:StudioView|undefined;

  function chairInAt(role:ActorRole):Point{return {x:STATIONS[role].x,y:STATIONS[role].y+CHAIR_IN};}
  function chairOutAt(role:ActorRole):Point{return {x:STATIONS[role].x,y:STATIONS[role].y+CHAIR_OUT};}
  function chairSideAt(role:ActorRole):Point{return {x:STATIONS[role].x+40,y:STATIONS[role].y+62};}
  function chairSeatOutAt(role:ActorRole):Point{return {x:STATIONS[role].x,y:STATIONS[role].y+CHAIR_OUT-CHAIR_IN};}
  function walkers():Walker[]{return [...Object.values(actors),...visitors.values(),player];}
  function propsMoving(){return Object.values(chairs).some(prop=>!propSettled(prop))||books.some(prop=>!propSettled(prop));}
  function timedPending(){return ROLES.some(role=>['dwell','face','sit','stand'].includes(actors[role].current?.kind||''));}
  function moving(){return walkers().some(item=>item.path.length>0)||propsMoving()||timedPending()||particles.active();}

  // ── Sequencer ──────────────────────────────────────────────────────────
  function send(item:Walker,point:Point,arrive?:()=>void){
    const path=findOfficePath(item,point);
    if(!path.length&&Math.hypot(item.x-point.x,item.y-point.y)>.001){
      if('role' in item)cancelScript(item as Actor);
      return;
    }
    item.activity='';item.onArrive=arrive;
    if(still){const end=path.at(-1);if(end){item.x=end.x;item.y=end.y;}item.path=[];item.frame=1;}
    else item.path=path;
    if(!item.path.length){const done=item.onArrive;item.onArrive=undefined;done?.();}
  }
  function finishStep(actor:Actor){actor.current=undefined;actor.stepElapsed=0;advance(actor);}
  function advance(actor:Actor){
    const step=actor.script.shift();
    if(!step){if(actor.role==='executor'){tripping=false;beginTrip();}return;}
    actor.current=step;actor.stepElapsed=0;
    if(step.kind==='walk'){send(actor,step.to,()=>finishStep(actor));return;}
    if(step.kind==='dwell'){if(still)finishStep(actor);return;}
    if(step.kind==='chair'){
      const chair=chairs[actor.role];
      const destination=step.action==='pull'||step.action==='roll-out'?chairOutAt(actor.role):chairInAt(actor.role);
      slideProp(chair,destination,still?0:CHAIR_SLIDE_MS);
      if(still){if(step.action.startsWith('roll-')){actor.x=chair.x;actor.y=chair.y-CHAIR_IN;}finishStep(actor);}
      return;
    }
    if(step.kind==='sit'||step.kind==='stand'){
      actor.poseFrom=actor.seatAmount;actor.frame=1;
      if(still){actor.seated=step.kind==='sit';actor.seatAmount=actor.seated?1:0;finishStep(actor);}
      return;
    }
    if(step.kind==='face'){
      const changed=actor.direction!==step.direction;actor.direction=step.direction;actor.frame=1;
      if(still||!changed)finishStep(actor);
      return;
    }
    if(step.kind==='activity'){actor.activity=step.name;finishStep(actor);return;}
    if(step.kind==='pick'){pickBook(step.book,actor);finishStep(actor);return;}
    if(step.kind==='shelve'){shelveBook(step.book,actor);finishStep(actor);return;}
  }
  function runScript(actor:Actor,steps:Step[]){cancelScript(actor);actor.script=steps;advance(actor);}
  function cancelScript(actor:Actor){
    actor.script=[];actor.current=undefined;actor.stepElapsed=0;actor.path=[];actor.onArrive=undefined;actor.frame=1;
    if(actor.station.seat){
      const chair=chairs[actor.role];
      // A seated actor keeps the chair at its interrupted position; an empty chair tucks back in.
      if(actor.seated||actor.seatAmount>0)placeProp(chair,chair);
      else slideProp(chair,chairInAt(actor.role),still?0:CHAIR_SLIDE_MS);
    }
  }
  function standSteps(actor:Actor):Step[]{
    return actor.station.seat&&(actor.seated||actor.seatAmount>0)?[
      {kind:'chair',action:'roll-out'},{kind:'stand'},{kind:'walk',to:chairSideAt(actor.role)},{kind:'chair',action:'push'}]:[];
  }
  function sitSteps(actor:Actor,activity:string,afterLeaving=false):Step[]{
    if(!actor.station.seat)return [{kind:'walk',to:actor.station},{kind:'face',direction:actor.station.face},{kind:'activity',name:activity}];
    if((actor.seated||actor.seatAmount>0)&&!afterLeaving)return [
      {kind:'face',direction:actor.station.face},{kind:'sit'},{kind:'chair',action:'roll-in'},{kind:'activity',name:activity}];
    return [{kind:'walk',to:chairSideAt(actor.role)},{kind:'chair',action:'pull'},{kind:'walk',to:chairSeatOutAt(actor.role)},
      {kind:'face',direction:actor.station.face},{kind:'sit'},{kind:'chair',action:'roll-in'},{kind:'activity',name:activity}];
  }
  function occupied(mode:ActorMode){return mode==='working'||(mode==='waiting');}

  // ── Books: desk (new agent material) → carried → floor stack by the shelf ──
  function restackDesk(){
    let n=0;bookState.forEach((value,slot)=>{if(value==='desk')placeProp(books[slot],{x:DESK_BOOK.x,y:DESK_BOOK.y-n++*5});});
  }
  function pickBook(slot:number,actor:Actor){
    if(bookState[slot]!=='desk')return;
    bookState[slot]='carried';carriedBook=slot;books[slot].kind=bookKind('bookUp',slot);actor.carrying=true;restackDesk();
  }
  function shelveBook(slot:number,actor:Actor){
    if(bookState[slot]!=='carried')return;
    actor.carrying=false;carriedBook=-1;bookState[slot]='shelf';books[slot].kind=bookKind('bookFlat',slot);
    placeProp(books[slot],SHELF_STACK(slot));
    if(!still){const stack=SHELF_STACK(slot);particles.burst('paper',stack.x,stack.y-8);}
  }
  function reconcileBooks(materials:OfficeMaterial[]){
    const visible=materials.slice(0,MAX_BOOKS),ids=visible.map(item=>item.material_id);
    if(ids.join('\0')===materialIds.join('\0'))return;
    if(tripping||carriedBook>=0)endTrip();
    const previous=new Map(materialIds.map((id,index)=>[id,{book:books[index],state:bookState[index]}]));
    books.length=0;bookState.length=0;materialIds.length=0;
    visible.forEach((item,slot)=>{
      const old=previous.get(item.material_id);
      const discovered=materialsReady&&!knownMaterials.has(item.material_id);
      const next=old?.state==='desk'?'desk':discovered&&item.origin==='agent'&&actors.executor.mode==='working'?'desk':'shelf';
      const book=old?.book||createProp(`book-${item.material_id}`,bookKind('bookFlat',slot),DESK_BOOK);
      book.kind=bookKind('bookFlat',slot);book.hidden=false;
      books.push(book);bookState.push(next);materialIds.push(item.material_id);
      if(next==='shelf')placeProp(book,SHELF_STACK(slot));
    });
    materials.forEach(item=>knownMaterials.add(item.material_id));
    materialsReady=true;restackDesk();
  }
  function endTrip(){
    tripping=false;const actor=actors.executor;cancelScript(actor);
    if(carriedBook>=0){
      bookState[carriedBook]='desk';books[carriedBook].kind=bookKind('bookFlat',carriedBook);
      placeProp(books[carriedBook],DESK_BOOK);restackDesk();carriedBook=-1;
    }
    actor.carrying=false;
  }
  function beginTrip(){
    const actor=actors.executor;
    if(tripping||actor.carrying||actor.script.length||actor.current||actor.path.length||actor.mode!=='working')return;
    const book=bookState.findIndex(value=>value==='desk');
    if(book<0||!findOfficePath(actor,SHELF_POINT).length)return;
    tripping=true;
    runScript(actor,[...standSteps(actor),
      {kind:'walk',to:DESK_FRONT},{kind:'face',direction:3},{kind:'pick',book},
      {kind:'walk',to:SHELF_POINT},{kind:'face',direction:3},{kind:'activity',name:'fetch'},
      {kind:'dwell',ms:SHELF_DWELL_MS},{kind:'shelve',book},{kind:'activity',name:''},
      ...sitSteps(actor,ACTIVITIES.executor,true)]);
  }

  // ── Visitors: sub-agents enter through the front door while live ────────
  function spawnVisitor(agent:SwarmVisitor){
    const used=new Set([...visitors.values()].map(item=>item.desk));
    const desk=VISITOR_DESKS.find(point=>!used.has(point))||VISITOR_DESKS[0];
    const visitor:Visitor={...walker(OUTSIDE_POINT,modelForRole(agent.role),3+visitors.size%3),direction:3,id:agent.id,role:ports.copy(roleTitleKey(agent.role||'')),status:agent.status,leaving:false,desk};
    visitors.set(agent.id,visitor);
    if(still){visitor.x=desk.x;visitor.y=desk.y;}
    else visitor.path=[{...DOOR_POINT},...findOfficePath(DOOR_POINT,desk)];
  }
  function admitVisitors(){
    for(const agent of desired.values()){
      if(visitors.size>=MAX_VISITORS)break;
      if(!visitors.has(agent.id))spawnVisitor(agent);
    }
  }
  function dismissVisitor(visitor:Visitor){
    if(visitor.leaving)return;
    visitor.leaving=true;
    const vanish=()=>{visitors.delete(visitor.id);admitVisitors();};
    const path=findOfficePath(visitor,DOOR_POINT);
    if(still||!path.length&&Math.hypot(visitor.x-DOOR_POINT.x,visitor.y-DOOR_POINT.y)>.001){visitor.path=[];vanish();return;}
    visitor.onArrive=vanish;visitor.path=[...path,{...OUTSIDE_POINT}];
  }
  function reconcileVisitors(list:SwarmVisitor[]){
    desired.clear();
    list.filter(agent=>agent.status==='running'||agent.status==='pending').slice(0,MAX_VISITORS).forEach(agent=>desired.set(agent.id,agent));
    [...visitors.values()].forEach(visitor=>{
      const agent=desired.get(visitor.id);
      if(!agent){dismissVisitor(visitor);return;}
      visitor.status=agent.status;visitor.role=ports.copy(roleTitleKey(agent.role||''));visitor.model=modelForRole(agent.role);
      if(visitor.leaving){visitor.leaving=false;visitor.onArrive=undefined;send(visitor,visitor.desk);}
    });
    admitVisitors();
  }

  // ── Resident intent from the projected view ────────────────────────────
  function applyActor(role:ActorRole,next:{mode:ActorMode;emote:Emote;taskId:string}){
    const actor=actors[role];
    const posture=`${next.mode}:${next.mode==='waiting'?state?.view.wait?.kind||'':''}`;
    const changed=actor.posture!==posture||actor.taskId!==next.taskId;
    actor.emote=next.emote;actor.taskId=next.taskId;
    if(!changed)return;
    const before=actor.mode;actor.mode=next.mode;actor.posture=posture;actor.toolRoute='';
    if(role==='executor')endTrip();else cancelScript(actor);
    actor.carrying=false;
    if(next.mode==='waiting'&&state?.view.wait?.kind==='human')
      runScript(actor,[...standSteps(actor),{kind:'walk',to:WAIT_POINTS[role]},{kind:'face',direction:0},{kind:'activity',name:'waiting'}]);
    else if(occupied(next.mode))runScript(actor,sitSteps(actor,next.mode==='waiting'?'waiting':ACTIVITIES[role]));
    else runScript(actor,[...standSteps(actor),{kind:'walk',to:actor.home},{kind:'face',direction:0},{kind:'activity',name:''}]);
    if(next.mode==='failed'&&before!=='failed'&&!still)particles.burst('puff',actor.x,actor.y-SPRITE_HEIGHT*.6);
  }

  function settleStill(){
    ROLES.forEach(role=>{
      const actor=actors[role],step=actor.current;
      if(!step)return;
      if(step.kind==='walk'){const end=actor.path.at(-1);if(end){actor.x=end.x;actor.y=end.y;}actor.path=[];actor.onArrive=undefined;actor.frame=1;}
      else if(step.kind==='chair'&&actor.station.seat){
        const chair=chairs[role];placeProp(chair,chair.to);
        if(step.action.startsWith('roll-')){actor.x=chair.x;actor.y=chair.y-CHAIR_IN;}
      }else if(step.kind==='sit'||step.kind==='stand'){actor.seated=step.kind==='sit';actor.seatAmount=actor.seated?1:0;}
      finishStep(actor);
    });
    visitors.forEach(visitor=>{
      const end=visitor.path.at(-1);if(end){visitor.x=end.x;visitor.y=end.y;}visitor.path=[];visitor.frame=1;
      const arrive=visitor.onArrive;visitor.onArrive=undefined;arrive?.();
    });
    const end=player.path.at(-1);if(end){player.x=end.x;player.y=end.y;}player.path=[];player.frame=1;
    Object.values(chairs).forEach(chair=>placeProp(chair,chair.to));
    books.forEach(book=>placeProp(book,book.to));
    particles.clear();
  }

  // ── Player ───────────────────────────────────────────────────────────────
  function steerPlayer(axis:Point,dt:number){
    if(!axis.x&&!axis.y){if(player.steered){player.steered=false;player.frame=1;}return;}
    player.path=[];player.target=undefined;player.steered=true;
    const distance=player.speed*dt/1000;
    const tryMove=(x:number,y:number)=>{if(walkable({x,y})){player.x=x;player.y=y;return true;}return false;};
    const nx=player.x+axis.x*distance,ny=player.y+axis.y*distance;
    if(!tryMove(nx,ny)&&!tryMove(nx,player.y))tryMove(player.x,ny);
    player.direction=facing(axis.x,axis.y);
    player.strideDistance=(player.strideDistance+distance)%STRIDE_DISTANCE;
    player.frame=WALK_FRAMES[Math.floor(player.strideDistance/(STRIDE_DISTANCE/4))];
  }
  function walkPlayer(point:Point,face?:number){
    const path=findOfficePath(player,point);
    if(!path.length)return false;
    player.target=point;player.targetAge=0;
    player.onArrive=face===undefined?undefined:()=>{player.direction=face;player.frame=1;};
    if(still){player.x=point.x;player.y=point.y;player.path=[];player.onArrive?.();player.onArrive=undefined;}
    else player.path=path;
    return true;
  }
  function nearestPlace():Place|undefined {
    let best:Place|undefined,distance=INTERACT_RANGE;
    for(const place of PLACES){
      const at=HOTSPOTS[place].approach,d=Math.hypot(at.x-player.x,at.y-player.y);
      if(d<distance){distance=d;best=place;}
    }
    return best;
  }

  // ── Hit testing ──────────────────────────────────────────────────────────
  function bodyRect(item:Walker):[number,number,number,number]{
    const height=SPRITE_HEIGHT*(1-.28*item.seatAmount);
    return [item.x-19,item.y-height,38,height];
  }
  function inside(rect:readonly number[],point:Point){return point.x>=rect[0]&&point.x<=rect[0]+rect[2]&&point.y>=rect[1]&&point.y<=rect[1]+rect[3];}
  function hit(point:Point):string {
    const people:Array<[string,Walker]>=[...ROLES.map(role=>[`actor:${role}`,actors[role]] as [string,Walker]),
      ...[...visitors.values()].filter(item=>!item.leaving).map(item=>[`visitor:${item.id}`,item] as [string,Walker])];
    people.sort((a,b)=>b[1].y-a[1].y);
    for(const [id,item] of people)if(inside(bodyRect(item),point))return id;
    for(const place of PLACES)if(inside(HOTSPOTS[place].rect,point))return `place:${place}`;
    return '';
  }
  function activate(id:string,source:'pointer'|'keyboard'){
    const [kind,value]=[id.slice(0,id.indexOf(':')),id.slice(id.indexOf(':')+1)];
    if(kind==='place'&&(PLACES as string[]).includes(value)){
      const place=value as Place;
      if(source==='pointer')walkPlayer(HOTSPOTS[place].approach,HOTSPOTS[place].face);
      ports.activate({kind:'place',place});
    }else if(kind==='actor'&&(ROLES as string[]).includes(value)){
      selected=value as ActorRole;ports.activate({kind:'actor',role:selected});
    }else if(kind==='visitor'&&visitors.has(value))ports.activate({kind:'visitor',id:value});
  }

  // ── Drawing ─────────────────────────────────────────────────────────────
  function lit(rect:readonly number[],color:string,alpha:number,ctx:CanvasRenderingContext2D){
    ctx.save();ctx.globalAlpha=alpha;ctx.fillStyle=color;ctx.globalCompositeOperation='screen';
    ctx.fillRect(rect[0],rect[1],rect[2],rect[3]);ctx.restore();
  }
  function drawWorld(frame:SceneFrame){
    const {ctx,painter,now,pixel}=frame,motionless=frame.still;
    if(art.complete&&art.naturalWidth)ctx.drawImage(art,0,0,WORLD.width,WORLD.height);
    else{ctx.fillStyle='#8a6440';ctx.fillRect(0,0,WORLD.width,WORLD.height);}
    const tick=(period:number)=>motionless?0:Math.floor(now/period)%2;
    const wait=view?.wait;
    // Screens light only from the stage that actually owns them.
    const executor=actors.executor;
    if(wait?.kind==='compute')lit(MONITORS.executor,wait.error?PALETTE.bad:PALETTE.info,tick(SCREEN_FRAME_MS)?.55:.3,ctx);
    else if(executor.mode==='working'||executor.mode==='waiting')lit(MONITORS.executor,PALETTE.good,tick(SCREEN_FRAME_MS)?.38:.26,ctx);
    if(actors.reviewer.mode==='working'||actors.reviewer.mode==='waiting')lit(MONITORS.reviewer,PALETTE.good,tick(SCREEN_FRAME_MS)?.38:.26,ctx);
    if(view?.deliverable){
      const glow=tick(DELIVERY_FRAME_MS)?.55:.32,{x,y}=DELIVERY_DOCUMENT;
      ctx.save();const gradient=ctx.createRadialGradient(x,y,2,x,y,34);
      gradient.addColorStop(0,`rgba(255,226,140,${glow})`);gradient.addColorStop(1,'rgba(255,226,140,0)');
      ctx.fillStyle=gradient;ctx.beginPath();ctx.arc(x,y,34,0,Math.PI*2);ctx.fill();ctx.restore();
    }
    for(const place of PLACES){
      const id=`place:${place}`;
      const tone=frame.hover===id?'hover':frame.focus===id?'focus':state?.attention.includes(place)?'attention':'';
      if(!tone)continue;
      if(tone==='attention'&&tick(ATTENTION_FRAME_MS))continue;
      painter.hotspot(ctx,HOTSPOTS[place].rect,pixel,now,tone);
    }
    books.forEach((book,slot)=>{if(bookState[slot]&&bookState[slot]!=='carried')drawProp(ctx,propAtlas,book,SPRITE_HEIGHT*PROP_SCALE.bookFlat);});
    if(player.target&&player.path.length)painter.moveTarget(ctx,player.target.x,player.target.y,pixel,player.targetAge%1400);
    const drawables:Array<{y:number;draw:()=>void}>=[];
    for(const role of seated)drawables.push({y:chairs[role].y,draw:()=>drawOfficeChair(ctx,chairAtlas,chairs[role].x,chairs[role].y,SPRITE_HEIGHT*.72)});
    const people:Array<[Walker,string,number]>=[...ROLES.map(role=>[actors[role],`actor:${role}`,1] as [Walker,string,number]),
      ...[...visitors.values()].map(item=>[item,`visitor:${item.id}`,item.y>DOOR_POINT.y?Math.max(0,1-(item.y-DOOR_POINT.y)/(OUTSIDE_POINT.y-DOOR_POINT.y)):1] as [Walker,string,number]),
      [player,'player',1]];
    for(const [item,id,alpha] of people)drawables.push({y:item.y,draw:()=>{
      if(alpha<=0)return;
      ctx.save();ctx.globalAlpha=alpha;
      painter.shadow(ctx,item.x,item.y-2,18-5*item.seatAmount,6);
      const ring=id===frame.hover||id===frame.focus||(id===`actor:${selected}`);
      if(ring){ctx.strokeStyle=PALETTE.gold;ctx.lineWidth=2*pixel;ctx.beginPath();ctx.ellipse(item.x,item.y-2,23,8,0,0,Math.PI*2);ctx.stroke();}
      characters.draw(ctx,item.model,item.sprite,item.direction,item.frame,item.x,item.y,SPRITE_HEIGHT,item.seatAmount);
      if((item as Actor).carrying&&carriedBook>=0&&books[carriedBook]){
        const book=books[carriedBook],side=item.direction===1?-1:1;
        book.x=item.x+13*side;book.y=item.y-SPRITE_HEIGHT*.42;
        drawProp(ctx,propAtlas,book,SPRITE_HEIGHT*PROP_SCALE.bookUp);
      }
      ctx.restore();
    }});
    drawables.sort((a,b)=>a.y-b.y).forEach(item=>item.draw());
    particles.draw(ctx);
  }
  function drawOverlay(frame:SceneFrame){
    const {ctx,painter,now,project}=frame,motionless=frame.still;
    const head=(item:Walker)=>project({x:item.x,y:item.y-SPRITE_HEIGHT*(1-.22*item.seatAmount)-2});
    for(const role of ROLES){
      const actor=actors[role],top=head(actor),id=`actor:${role}`;
      if(actor.tool&&actor.mode==='working')painter.speech(ctx,actor.tool,top.x,top.y-(actor.emote?26:0));
      if(actor.emote)painter.emote(ctx,actor.emote,top.x,top.y,now,motionless);
      if(frame.hover===id||frame.focus===id||selected===role)painter.plate(ctx,state?.names[role]||role,top.x,top.y-(actor.emote?30:4)-(actor.tool&&actor.mode==='working'?28:0));
    }
    for(const visitor of visitors.values()){
      if(visitor.leaving)continue;
      const top=head(visitor),id=`visitor:${visitor.id}`;
      if(visitor.status==='running')painter.emote(ctx,'work',top.x,top.y,now,motionless);
      if(frame.hover===id||frame.focus===id)painter.plate(ctx,visitor.role,top.x,top.y-(visitor.status==='running'?30:4),'muted');
    }
    const top=head(player);
    painter.marker(ctx,top.x,top.y);
    if(!player.path.length&&!player.steered){
      const near=nearestPlace();
      if(near){const anchor=project(HOTSPOTS[near].anchor);painter.keycap(ctx,'E',anchor.x,anchor.y);}
    }
  }

  // ── Scene contract ──────────────────────────────────────────────────────
  const scene:Scene={
    width:WORLD.width,height:WORLD.height,
    backdrop(){return backdrop;},
    step(dt,input){
      const beats=Object.fromEntries(ROLES.map(role=>[role,actors[role].current]));
      stepProps(Object.values(chairs),dt);stepProps(books,dt);
      for(const role of seated){
        const actor=actors[role],step=actor.current,chair=chairs[role];
        if(step?.kind!=='chair')continue;
        if(step.action.startsWith('roll-')){actor.x=chair.x;actor.y=chair.y-CHAIR_IN;}
        if(propSettled(chair))finishStep(actor);
      }
      for(const item of walkers()){
        if('role' in item&&(item as Actor).current!==beats[(item as Actor).role])continue;
        if(stepWalker(item,dt,item.speed)){const arrive=item.onArrive;item.onArrive=undefined;arrive?.();}
      }
      for(const role of ROLES){
        const actor=actors[role],step=actor.current;
        if(!step||step!==beats[role])continue;
        if(step.kind==='sit'||step.kind==='stand'){
          actor.stepElapsed+=dt;actor.seatAmount=seatPose(actor.poseFrom,step.kind==='sit'?1:0,actor.stepElapsed);
          if(actor.stepElapsed>=SEAT_TRANSITION_MS){actor.seated=step.kind==='sit';finishStep(actor);}
        }else if(step.kind==='dwell'||step.kind==='face'){
          actor.stepElapsed+=dt;
          if(actor.stepElapsed>=(step.kind==='face'?TURN_MS:step.ms))finishStep(actor);
        }
      }
      steerPlayer(input.axis,dt);
      // The move ring is drawn only while walking; arrival retires it.
      if(player.target){if(player.path.length)player.targetAge+=dt;else player.target=undefined;}
      particles.step(dt);
    },
    draw(frame){
      frame.world();drawWorld(frame);
      frame.screen();drawOverlay(frame);
    },
    nextWake(now,motionless){
      if(moving())return 0;
      if(motionless)return Infinity;
      const periods:number[]=[];
      for(const role of ROLES){const period=emoteFrameMs(actors[role].emote);if(period)periods.push(period);}
      if([...visitors.values()].some(item=>item.status==='running'&&!item.leaving))periods.push(emoteFrameMs('work'));
      if(view?.wait?.kind==='compute'||ROLES.some(role=>role!=='coordinator'&&['working','waiting'].includes(actors[role].mode)))periods.push(SCREEN_FRAME_MS);
      if(view?.deliverable)periods.push(DELIVERY_FRAME_MS);
      if(state?.attention.length)periods.push(ATTENTION_FRAME_MS);
      return Math.min(...periods.map(period=>period-(now%period)));
    },
    hit,
    targets(){
      const targets:StageTarget[]=PLACES.map(place=>{
        const [x,y,width,height]=HOTSPOTS[place].rect;
        const data:Record<string,string>={place};
        if(place==='library'){data.books=bookState.join(',');data.materialIds=materialIds.join(',');}
        if(place==='work'||place==='review'){
          const role=place==='work'?'executor':'reviewer',chair=chairs[role];
          data.chair=propSettled(chair)?(chair.y>chairInAt(role).y+1?'out':'in'):'moving';
        }
        return {id:`place:${place}`,label:state?.placeLabels[place]||place,x,y,width,height,data};
      });
      for(const role of ROLES){
        const actor=actors[role],[x,y,width,height]=bodyRect(actor);
        targets.push({id:`actor:${role}`,label:state?.actorLabels[role]||role,x,y,width,height,pressed:selected===role,
          data:{actor:role,mode:actor.mode,emote:actor.emote,phase:actor.path.length?'walk':actor.current?.kind||(actor.seated?'seated':'idle'),
            seated:String(actor.seated),activity:actor.activity,carrying:String(actor.carrying),model:actor.model,tool:actor.tool}});
      }
      for(const visitor of visitors.values()){
        if(visitor.leaving)continue;
        const [x,y,width,height]=bodyRect(visitor);
        targets.push({id:`visitor:${visitor.id}`,label:`${visitor.role} · ${ports.copy(visitor.status==='running'?'mode_working':'pending')}`,x,y,width,height,
          data:{visitor:visitor.id,status:visitor.status,model:visitor.model}});
      }
      return targets;
    },
    activate,
    tapFloor(point){walkPlayer(point);},
    interact(){const place=nearestPlace();if(place)activate(`place:${place}`,'keyboard');},
    /** Point of interest for a camera that cannot show the whole room. */
    follow(){
      const focus:Point=player.steered||player.path.length?player
        :view?.wait?.kind==='human'?WAIT_POINTS.executor
        :['working','waiting','failed'].includes(actors.executor.mode)?actors.executor
        :['working','waiting'].includes(actors.reviewer.mode)?actors.reviewer
        :view?.deliverable&&view.phase==='completed'?DELIVERY_DOCUMENT
        :actors.coordinator.mode==='working'?actors.coordinator:player;
      return {x:focus.x,y:focus.y-SPRITE_HEIGHT/2};
    },
    setStill(next){still=next;if(still)settleStill();},
  };

  art.onload=()=>{if(disposed)return;backdrop=makeBackdrop(art,art.naturalWidth,art.naturalHeight);ports.wake();};
  propAtlas.onload=chairAtlas.onload=()=>{if(!disposed)ports.wake();};
  art.src=officeArt;propAtlas.src=propsArt;chairAtlas.src=chairArt;
  ROLES.forEach(role=>characters.load(actors[role].model));characters.load(player.model);

  return {
    scene,
    update(next:OfficeState){
      if(disposed)return;
      if(next.goalId!==goalId){
        endTrip();goalId=next.goalId;assignmentSeq=next.assignmentSeq;
        ROLES.forEach(role=>{actors[role].tool='';actors[role].toolRoute='';});
        books.length=0;bookState.length=0;materialIds.length=0;knownMaterials.clear();materialsReady=false;
      }
      const previous=view;
      state=next;view=next.view;
      ROLES.forEach(role=>applyActor(role,next.view.actors[role]));
      reconcileBooks(next.materials);
      // A finished executor leaves no material mid-trip: the durable shelf is authoritative.
      if(actors.executor.mode!=='working')bookState.forEach((value,slot)=>{
        if(value==='desk'){bookState[slot]='shelf';placeProp(books[slot],SHELF_STACK(slot));}
      });
      if(next.assignmentSeq>assignmentSeq){
        assignmentSeq=next.assignmentSeq;
        const coordinator=actors.coordinator;
        runScript(coordinator,[{kind:'walk',to:HANDOFF_POINT},{kind:'face',direction:1},{kind:'activity',name:'talk'},
          {kind:'dwell',ms:HANDOFF_DWELL_MS},{kind:'walk',to:coordinator.mode==='working'?coordinator.station:coordinator.home},
          {kind:'face',direction:coordinator.mode==='working'?coordinator.station.face:0},
          {kind:'activity',name:coordinator.mode==='working'?ACTIVITIES.coordinator:''}]);
      }
      reconcileVisitors(next.agents);
      beginTrip();
      // Discrete celebration when an accepted deliverable first lands on the table.
      if(previous&&previous.runId===next.view.runId&&!still&&next.view.verdict==='pass'&&previous.verdict!=='pass')
        particles.burst('sparkle',DELIVERY_DOCUMENT.x,DELIVERY_DOCUMENT.y-10);
    },
    /** Live tool chip over one resident, fed by the task activity stream. */
    setToolBubble(role:ActorRole,tool:string){
      if(disposed)return;
      const actor=actors[role];actor.tool=tool;
      const route=tool?toolRoute(tool):undefined;
      if(route&&actor.mode==='working'&&!tripping&&actor.toolRoute!==tool){
        actor.toolRoute=tool;
        runScript(actor,[...standSteps(actor),{kind:'walk',to:route.point},{kind:'face',direction:route.face},{kind:'activity',name:route.activity}]);
        return;
      }
      if(actor.toolRoute&&actor.toolRoute!==tool){
        actor.toolRoute='';
        if(actor.mode==='working'&&!tripping)runScript(actor,sitSteps(actor,ACTIVITIES[role]));
      }
    },
    select(role:ActorRole|''){selected=role;},
    diagnostics(){
      return {books:bookState.join(','),materialIds:materialIds.join(','),assignmentSeq:String(assignmentSeq),
        visitors:[...visitors.keys()].join(','),player:`${player.x},${player.y}`,
        chairs:seated.map(role=>propSettled(chairs[role])?(chairs[role].y>chairInAt(role).y+1?'out':'in'):'moving').join(',')};
    },
    dispose(){
      if(disposed)return;disposed=true;
      ROLES.forEach(role=>cancelScript(actors[role]));visitors.clear();desired.clear();particles.clear();characters.dispose();
      art.onload=null;propAtlas.onload=null;chairAtlas.onload=null;
    },
  };
}
export type OfficeScene = ReturnType<typeof createOfficeScene>;
