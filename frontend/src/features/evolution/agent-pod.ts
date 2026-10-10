/** Selected department room, painted from authored scenery and live occupancy.
 * The role selects scenery; the roster's status drives the actor pose and
 * monitor glow, and its objective is the only brief in the room. Decorative
 * furniture never stands for a saved material, task result or verification.
 * Canvas 2D, dirty and bounded: only visible status-transition choreography
 * animates; a stationary working actor never cycles walking frames.
 * Sprite/prop atlases shared with the main office scene.
 */
import chairArt from './assets/game-chair.webp';
import { ROOM_ART, themeForRole, type PodTheme } from './room-catalog';
import { esc } from './components/ui';
import { FLOOR_WIDTH, FLOOR_HEIGHT } from './office-navigation';
import { createOfficeCanvasSurface } from './office-canvas';
import { createPodMotion, targetPodMotion, stepPodMotion } from './office-pod-motion';
import { modelForRole } from './office-character';
import { createCharacterPainter } from './office-character-art';
import { drawOfficeChair } from './office-objects';
export type PodAgent = {id:string;role:string;status:string;objective?:string};
// Muted accents cycle per room: teal, crimson, amber, indigo (rug, mug, pin, glow).
const ACCENTS = ['#4d7a72','#a14a4a','#c08a3e','#5a5f8c'];
const WOOD_DARK = '#463522', WOOD_BASE = '#6b4f35', WOOD_LIGHT = '#7d5f3f';
const STATUS_GLOW:Record<string,string> = {running:'#85d69d',pending:'#ffd16f'};
const STATUS_KEYS = ['running','pending','completed','failed','cancelled','needs_attention','idle'];
const statusKey = (status:string) => STATUS_KEYS.includes(status)?status:status==='done'?'completed':'idle';
// Desk composition anchors (scene px, 960×720 like the main office floor).
const DESK = {left:330,right:630,top:420,edge:452,legBottom:600};
const CHAIR_AT = {x:480,y:502};
export function mountAgentPod(host:HTMLElement, options:{
  copy:(key:string)=>string;openTask:()=>void;motionPaused?:()=>boolean;briefHost?:HTMLElement;
}) {
  const {copy}=options;
  host.innerHTML=`<canvas aria-hidden="true"></canvas><div class="evo-pod-actors"></div>
    <aside class="evo-pod-brief"><header><strong></strong><span class="evo-pod-status"></span></header>
    <p class="evo-pod-objective"></p><small>${esc(copy('agentRoomScope'))}</small></aside>`;
  const canvas=host.querySelector('canvas')!;
  const surface=createOfficeCanvasSurface(canvas),ctx=surface.ctx;
  const brief=host.querySelector<HTMLElement>('.evo-pod-brief')!;
  options.briefHost?.append(brief);
  const button=document.createElement('button');button.type='button';button.className='evo-person evo-pod-person';
  button.innerHTML='<span class="evo-person-label"></span><span class="evo-person-activity" aria-hidden="true"></span>';
  host.querySelector('.evo-pod-actors')!.append(button);
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const chairAtlas=new window.Image();
  const roomAtlas=new window.Image();
  const characterArt=createCharacterPainter(()=>{surface.invalidate();draw();});
  let occupied=false;
  let agent:PodAgent={id:'',role:'',status:'pending'};
  let sprite=3,tint=0,theme:PodTheme='general',currentArt='',requestedArt='',disposed=false,intersecting=true,wake=0,spriteHeight=104;
  let motion=createPodMotion('idle'),lastTick=0;
  let chairBeat:'arrive'|'leave'|undefined;
  function visible(){return !disposed&&!host.hidden&&!document.hidden&&intersecting;}
  function motionAllowed(){return !reduced?.matches&&!options.motionPaused?.();}
  function ensureArt(){
    if(!visible())return;
    if(occupied)characterArt.load(modelForRole(agent.role));
    if(!chairAtlas.src)chairAtlas.src=chairArt;
    if(currentArt&&requestedArt!==currentArt){requestedArt=currentArt;roomAtlas.src=currentArt;}
  }
  // These are room fixtures, never evidence or material records. Only the
  // main office's durable material projection creates collectible books.
  function drawSpecialty(accent:string){
    if(!ctx)return;
    if(theme==='engineering'){
      // Server rack and a second screen make this a coding bay.
      ctx.fillStyle=WOOD_DARK;ctx.fillRect(656,272,130,322);
      ctx.fillStyle='#2e2a24';ctx.fillRect(668,282,106,294);
      for(let i=0;i<6;i++){ctx.fillStyle='#494c48';ctx.fillRect(678,294+i*46,86,35);ctx.fillStyle=accent;ctx.fillRect(690,306+i*46,8,8);}
      ctx.fillStyle='#2e2a24';ctx.fillRect(310,256,132,88);ctx.fillStyle=accent;ctx.fillRect(318,264,116,72);
      ctx.fillStyle='#2e2a24';ctx.fillRect(370,344,12,30);
    }else if(theme==='review'){
      // Filing cabinet and a check matrix, with no claim about outcomes.
      ctx.fillStyle=WOOD_DARK;ctx.fillRect(654,346,136,246);
      for(let i=0;i<3;i++){ctx.fillStyle='#ded4bb';ctx.fillRect(664,356+i*78,116,68);ctx.fillStyle=WOOD_BASE;ctx.fillRect(710,385+i*78,24,5);}
      ctx.strokeStyle=accent;ctx.lineWidth=5;
      for(let i=0;i<3;i++){ctx.beginPath();ctx.rect(394,112+i*25,12,12);ctx.stroke();ctx.beginPath();ctx.moveTo(416,118+i*25);ctx.lineTo(520,118+i*25);ctx.stroke();}
    }else if(theme==='writing'){
      // Draft sheets and a side table make the room a writing studio.
      ctx.fillStyle=WOOD_LIGHT;ctx.fillRect(654,424,144,20);ctx.fillStyle=WOOD_BASE;ctx.fillRect(664,444,14,132);ctx.fillRect(778,444,14,132);
      for(let i=0;i<3;i++){ctx.fillStyle='#f8f2df';ctx.fillRect(670+i*23,394-i*10,76,40);ctx.fillStyle=accent;ctx.fillRect(680+i*23,406-i*10,52,3);}
      ctx.fillStyle=accent;ctx.fillRect(388,112,142,5);ctx.fillRect(388,136,110,5);ctx.fillRect(388,160,128,5);
    }else if(theme==='analysis'){
      // A chart wall and data cabinet describe the analyst's workspace.
      ctx.fillStyle=WOOD_DARK;ctx.fillRect(652,276,138,320);ctx.fillStyle='#efe6d0';ctx.fillRect(664,288,114,184);
      for(let i=0;i<4;i++){ctx.fillStyle=accent;ctx.fillRect(676+i*24,442-i*31,14,20+i*31);}
      ctx.fillStyle=WOOD_LIGHT;for(let i=0;i<3;i++)ctx.fillRect(662,492+i*31,118,23);
      ctx.strokeStyle=accent;ctx.lineWidth=4;ctx.beginPath();ctx.moveTo(392,174);ctx.lineTo(424,138);ctx.lineTo(456,154);ctx.lineTo(498,112);ctx.lineTo(532,128);ctx.stroke();
    }else{
      // A quiet reference shelf for researchers and unspecified roles.
      ctx.fillStyle=WOOD_BASE;ctx.fillRect(660,300,120,300);
      ctx.fillStyle=WOOD_DARK;ctx.fillRect(668,310,104,282);
      for(let s=0;s<3;s++){ctx.fillStyle=WOOD_LIGHT;ctx.fillRect(668,392+s*92,104,8);}
      for(let i=0;i<(theme==='research'?10:4);i++){
        ctx.fillStyle=i%2?accent:'#d8cdb8';ctx.fillRect(678+(i%5)*17,350+Math.floor(i/5)*92,10,40);
      }
      ctx.fillStyle='#5c7a4e';ctx.beginPath();ctx.ellipse(720,278,16,22,0,0,Math.PI*2);ctx.ellipse(704,286,10,14,0,0,Math.PI*2);ctx.fill();
      ctx.fillStyle=accent;ctx.fillRect(706,292,28,10);
      if(theme==='research'){
        ctx.strokeStyle=accent;ctx.lineWidth=4;ctx.beginPath();ctx.arc(460,148,32,0,Math.PI*2);ctx.moveTo(428,148);ctx.lineTo(492,148);ctx.moveTo(460,116);ctx.lineTo(460,180);ctx.stroke();
      }
    }
  }
  function drawRoom(){
    if(!ctx)return;
    const tintColor=ACCENTS[tint%ACCENTS.length];
    if(roomAtlas.complete&&roomAtlas.naturalWidth){
      ctx.drawImage(roomAtlas,0,0,FLOOR_WIDTH,FLOOR_HEIGHT);
      if(theme==='analysis'){
        // Unlabelled graph paper is room decor, not a result or token chart.
        ctx.fillStyle='#f8f2df';ctx.fillRect(466,28,78,54);
        ctx.strokeStyle='#caa46e';ctx.lineWidth=1;
        for(let x=476;x<544;x+=10){ctx.beginPath();ctx.moveTo(x,34);ctx.lineTo(x,76);ctx.stroke();}
        for(let y=36;y<82;y+=10){ctx.beginPath();ctx.moveTo(472,y);ctx.lineTo(538,y);ctx.stroke();}
      }
      // The background monitor is blank. Its tint comes only from the roster
      // status, not from the room theme or a guessed tool result.
      if(agent.status==='running'||agent.status==='pending'){
        ctx.fillStyle=STATUS_GLOW[agent.status];ctx.globalAlpha=.22;
        ctx.fillRect(438,348,72,40);ctx.globalAlpha=1;
      }
      drawOccupant();
      return;
    }
    // Wall, wainscot and baseboard.
    ctx.fillStyle='#ecdfc6';ctx.fillRect(0,0,FLOOR_WIDTH,368);
    ctx.fillStyle='#8f6f4d';ctx.fillRect(0,330,FLOOR_WIDTH,30);
    ctx.fillStyle=WOOD_BASE;ctx.fillRect(0,356,FLOOR_WIDTH,12);
    // Floor planks.
    for(let y=368,row=0;y<FLOOR_HEIGHT;y+=44,row++){
      ctx.fillStyle=row%2?'#bb9c74':'#c4a67e';ctx.fillRect(0,y,FLOOR_WIDTH,44);
      ctx.fillStyle='#a4876380';ctx.fillRect(0,y,FLOOR_WIDTH,2);
      ctx.fillRect((row*263)%FLOOR_WIDTH,y,2,44);
    }
    // Window with morning light.
    ctx.fillStyle=WOOD_DARK;ctx.fillRect(88,66,168,168);
    const sky=ctx.createLinearGradient(0,72,0,228);sky.addColorStop(0,'#dfe9ea');sky.addColorStop(1,'#f5ead0');
    ctx.fillStyle=sky;ctx.fillRect(96,74,152,152);
    ctx.fillStyle=WOOD_DARK;ctx.fillRect(168,74,8,152);ctx.fillRect(96,144,152,8);
    ctx.fillStyle='#ffffff24';ctx.beginPath();ctx.moveTo(120,234);ctx.lineTo(220,234);ctx.lineTo(330,560);ctx.lineTo(180,560);ctx.closePath();ctx.fill();
    // Brief board: the agent's own pinned assignment (text lives in the DOM card).
    ctx.fillStyle=WOOD_BASE;ctx.fillRect(330,66,260,146);
    ctx.fillStyle='#caa46e';ctx.fillRect(338,74,244,130);
    ctx.fillStyle='#f8f2df';ctx.fillRect(382,96,156,96);
    ctx.fillStyle=tintColor;ctx.beginPath();ctx.arc(460,96,7,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#a1947c';
    for(let i=0;i<4;i++)ctx.fillRect(396,112+i*18,128-(i%2)*34,6);
    // Door back to the main office (right wall).
    ctx.fillStyle=WOOD_DARK;ctx.fillRect(806,136,96,232);
    ctx.fillStyle=WOOD_BASE;ctx.fillRect(814,144,80,224);
    ctx.fillStyle='#00000014';ctx.fillRect(830,160,48,84);
    ctx.fillStyle='#d9c9a0';ctx.beginPath();ctx.arc(826,268,5,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#f8f2df';ctx.fillRect(828,196,52,20);
    drawSpecialty(tintColor);
    // Rug in the room tint.
    ctx.fillStyle=tintColor+'47';ctx.beginPath();ctx.roundRect(290,510,380,170,26);ctx.fill();
    ctx.strokeStyle=tintColor+'8c';ctx.lineWidth=3;ctx.beginPath();ctx.roundRect(302,522,356,146,20);ctx.stroke();
    // Desk, monitor (status glow), keyboard, mug.
    ctx.fillStyle=WOOD_LIGHT;ctx.fillRect(DESK.left,DESK.top,DESK.right-DESK.left,20);
    ctx.fillStyle='#5f4530';ctx.fillRect(DESK.left,DESK.top+20,DESK.right-DESK.left,DESK.edge-DESK.top-20);
    ctx.fillRect(345,DESK.edge,15,DESK.legBottom-DESK.edge);ctx.fillRect(600,DESK.edge,15,DESK.legBottom-DESK.edge);
    const glow=STATUS_GLOW[agent.status]||'#8f968f';
    ctx.fillStyle='#2e2a24';ctx.fillRect(438,346,112,72);
    ctx.fillStyle=glow;ctx.globalAlpha=agent.status==='running'?.92:.55;ctx.fillRect(444,352,100,56);ctx.globalAlpha=1;
    ctx.fillStyle='#2e2a2440';for(let i=0;i<4;i++)ctx.fillRect(444,358+i*13,100,3);
    ctx.fillStyle='#2e2a24';ctx.fillRect(486,418,16,10);ctx.fillRect(468,428,52,6);
    ctx.fillStyle='#d8cdb8';ctx.fillRect(452,436,84,10);ctx.fillStyle='#00000018';ctx.fillRect(452,441,84,2);
    ctx.fillStyle=tintColor;ctx.fillRect(576,412,18,18);ctx.fillStyle='#00000022';ctx.fillRect(576,412,18,5);
    // Pendant lamp over the desk.
    ctx.strokeStyle=WOOD_DARK;ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(480,0);ctx.lineTo(480,66);ctx.stroke();
    ctx.fillStyle=WOOD_DARK;ctx.beginPath();ctx.moveTo(452,96);ctx.lineTo(508,96);ctx.lineTo(494,66);ctx.lineTo(466,66);ctx.closePath();ctx.fill();
    if(agent.status==='running'){ctx.fillStyle='#fff3c41f';ctx.beginPath();ctx.moveTo(452,96);ctx.lineTo(508,96);ctx.lineTo(560,420);ctx.lineTo(400,420);ctx.closePath();ctx.fill();}
    // One upholstered chair shared with the main office.
    drawOccupant();
  }
  function drawActor(){
    if(!ctx||!occupied)return;
    ctx.fillStyle='rgba(42,32,21,.23)';ctx.beginPath();ctx.ellipse(motion.x,motion.y-2,motion.seated?14:19,6,0,0,Math.PI*2);ctx.fill();
    characterArt.draw(ctx,modelForRole(agent.role),sprite,motion.direction,motion.frame,
      motion.x,motion.y,spriteHeight,motion.seatAmount);
  }
  function drawOccupant(){
    if(!ctx)return;
    if(motion.y<=motion.chairY)drawActor();
    drawOfficeChair(ctx,chairAtlas,CHAIR_AT.x,motion.chairY,spriteHeight*.72);
    if(motion.y>motion.chairY)drawActor();
  }
  function draw(){
    if(!visible())return;
    ensureArt();host.dataset.chairBeat=chairBeat||'';
    surface.paint(JSON.stringify([occupied,agent.status,agent.role,theme,tint,sprite,motion.x,motion.y,motion.frame,motion.direction,motion.seatAmount,motion.chairY]),()=>drawRoom());
    syncButton();
  }
  let buttonSignature='';
  function syncButton(){

    const signature=JSON.stringify([motion.x,motion.y,motion.frame,motion.current?.kind,motion.seated,spriteHeight,agent.status,agent.role]);
    if(signature===buttonSignature)return;
    buttonSignature=signature;
    button.style.left=`${motion.x/FLOOR_WIDTH*100}%`;
    button.style.top=`${motion.y/FLOOR_HEIGHT*100}%`;
    button.style.height=`${spriteHeight/FLOOR_HEIGHT*100}%`;
    button.dataset.phase=motion.current?.kind||'idle';
    button.dataset.frame=String(motion.frame);
    button.dataset.seated=String(motion.seated);
    button.dataset.status=agent.status;
    button.dataset.activity=agent.status==='running'?'working':'';
    button.dataset.model=modelForRole(agent.role);
    button.setAttribute('aria-label',`${agent.role} · ${copy(statusKey(agent.status))}`);
    button.title=button.getAttribute('aria-label')!;
    button.firstElementChild!.textContent=agent.role;
  }
  function resize(){
    if(disposed)return;
    const width=host.clientWidth||640;
    surface.resize(width,window.devicePixelRatio||1);
    draw();
  }
  function stop(){window.clearTimeout(wake);wake=0;lastTick=0;}
  function tick(){
    wake=0;if(!visible()||!motionAllowed())return;
    const now=window.performance.now(),dt=lastTick?Math.min(60,now-lastTick):1000/30;lastTick=now;
    stepPodMotion(motion,dt);
    if(!motion.current)chairBeat=undefined;
    draw();
    if(motion.current)wake=window.setTimeout(tick,1000/30);
  }
  function animate(){
    if(disposed)return;
    stop();
    if(!motionAllowed()){targetPodMotion(motion,agent.status,false);chairBeat=undefined;}
    draw();if(motion.current&&visible())wake=window.setTimeout(tick,1000/30);
  }
  function visibility(){if(visible())animate();else stop();}
  const resizeObserver=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):undefined;
  resizeObserver?.observe(host);
  const observer=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{intersecting=entries[0]?.isIntersecting??false;visibility();}):undefined;
  observer?.observe(host);
  document.addEventListener('visibilitychange',visibility);
  reduced?.addEventListener('change',visibility);
  const openTask=()=>{if(!disposed)options.openTask();};
  button.addEventListener('click',openTask);
  roomAtlas.onload=()=>{if(disposed)return;host.dataset.art='ready';surface.invalidate();draw();};
  roomAtlas.onerror=()=>{if(disposed)return;host.dataset.art='unavailable';surface.invalidate();draw();};
  chairAtlas.onload=()=>{if(!disposed){surface.invalidate();draw();}};
  resize();
  return {
    update(member:PodAgent|null,visual:{sprite:number;accent:number;theme?:PodTheme;title?:string;known?:boolean}){
      if(disposed)return;
      occupied=!!member;
      const next=member||{id:'',role:'',status:'idle'};
      if(agent.id!==next.id){motion=createPodMotion(next.status);chairBeat=undefined;}
      else if(agent.status!==next.status){
        targetPodMotion(motion,next.status,motionAllowed());
        chairBeat=motion.current?(next.status==='running'?'arrive':'leave'):undefined;
      }
      agent=next;sprite=visual.sprite;tint=visual.accent;theme=visual.theme||themeForRole(next.role);
      button.hidden=!occupied;
      host.dataset.status=next.status;
      host.dataset.theme=theme;
      const art=ROOM_ART[theme];
      if(currentArt!==art){currentArt=art;host.dataset.art='loading';surface.invalidate();}
      brief.querySelector('strong')!.textContent=occupied?next.role:visual.title||'';
      const chip=brief.querySelector<HTMLElement>('.evo-pod-status')!;
      chip.textContent=copy(occupied?statusKey(next.status):visual.known===false?'roomRosterUnknown':'roomUnoccupied');chip.dataset.status=next.status;
      brief.querySelector('.evo-pod-objective')!.textContent=occupied?(next.objective||copy('agentRoomNoBrief')):copy(visual.known===false?'roomRosterHint':'roomEmptyHint');
      brief.querySelector('small')!.hidden=!occupied;
      // DOM facts land immediately; only the actor transition waits for frames.
      syncButton();
      animate();
    },
    refreshMotion:animate,
    metrics:surface.metrics,
    dispose(){
      if(disposed)return;disposed=true;stop();

      button.removeEventListener('click',openTask);
      resizeObserver?.disconnect();observer?.disconnect();
      document.removeEventListener('visibilitychange',visibility);
      reduced?.removeEventListener('change',visibility);
      brief.remove();characterArt.dispose();chairAtlas.onload=null;roomAtlas.onload=null;roomAtlas.onerror=null;surface.dispose();
    },
  };
}
