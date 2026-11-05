/** Department room scene for one rostered sub-agent, on the shared game stage.
 * The agent's role selects authored scenery; its roster status alone drives the
 * pose, chair beat, screen glow and emote. Decorative furniture never stands
 * for a material, result or verification. A same-agent status change plays one
 * bounded arrive/leave chair sequence; first sight settles immediately.
 * Entry: createPodScene. The stage owns loop, camera and input.
 */
import chairArt from './assets/game-chair.webp';
import { ROOM_ART, type PodTheme } from './room-catalog';
import { modelForRole } from './office-character';
import { createCharacterPainter } from './office-character-art';
import { drawOfficeChair } from './office-objects';
import { createPodMotion, targetPodMotion, stepPodMotion, type PodMotion } from './office-pod-motion';
import { emoteFrameMs, makeBackdrop, PALETTE } from './engine/paint';
import type { Scene, StageTarget } from './engine/stage';

export type PodAgent = {id:string;role:string;status:string;objective?:string};
const WIDTH = 960, HEIGHT = 720, SPRITE_HEIGHT = 104;
const ACCENTS = ['#4d7a72','#a14a4a','#c08a3e','#5a5f8c'];
const STATUS_GLOW:Record<string,string> = {running:'#85d69d',pending:'#ffd16f'};
const CHAIR_X = 480;

export function createPodScene(ports:{wake:()=>void;openMember:()=>void;statusLabel:(status:string)=>string;roleLabel:(role:string)=>string}) {
  const room=new window.Image(),chairAtlas=new window.Image();
  const characters=createCharacterPainter(()=>{if(!disposed)ports.wake();});
  let agent:PodAgent|undefined,theme:PodTheme='general',accent=0,requested='',disposed=false,still=false;
  let motion:PodMotion=createPodMotion('idle'),backdrop:HTMLCanvasElement|undefined,beat:''|'arrive'|'leave'='';
  function bodyRect():[number,number,number,number]{
    const height=SPRITE_HEIGHT*(1-.28*motion.seatAmount);return [motion.x-24,motion.y-height,48,height];
  }
  function drawFallback(ctx:CanvasRenderingContext2D){
    // Network-failure scenery: wall, floor planks, desk and screen in the room accent.
    const tint=ACCENTS[accent%ACCENTS.length];
    ctx.fillStyle='#ecdfc6';ctx.fillRect(0,0,WIDTH,368);
    ctx.fillStyle='#8f6f4d';ctx.fillRect(0,330,WIDTH,30);
    for(let y=368,row=0;y<HEIGHT;y+=44,row++){ctx.fillStyle=row%2?'#bb9c74':'#c4a67e';ctx.fillRect(0,y,WIDTH,44);}
    ctx.fillStyle='#7d5f3f';ctx.fillRect(330,420,300,20);ctx.fillStyle='#5f4530';ctx.fillRect(330,440,300,12);
    ctx.fillRect(345,452,15,148);ctx.fillRect(600,452,15,148);
    ctx.fillStyle='#2e2a24';ctx.fillRect(438,346,112,72);ctx.fillStyle=tint;ctx.fillRect(444,352,100,56);
  }
  function drawActor(ctx:CanvasRenderingContext2D){
    if(!agent)return;
    ctx.fillStyle=PALETTE.shadow;ctx.beginPath();ctx.ellipse(motion.x,motion.y-2,motion.seated?14:19,6,0,0,Math.PI*2);ctx.fill();
    characters.draw(ctx,modelForRole(agent.role),3,motion.direction,motion.frame,motion.x,motion.y,SPRITE_HEIGHT,motion.seatAmount);
  }
  const scene:Scene={
    width:WIDTH,height:HEIGHT,
    backdrop(){return backdrop;},
    step(dt){if(!still&&motion.current){stepPodMotion(motion,dt);if(!motion.current)beat='';}},
    draw(frame){
      const {ctx,now,painter,project}=frame;
      frame.world();
      if(room.complete&&room.naturalWidth)ctx.drawImage(room,0,0,WIDTH,HEIGHT);else drawFallback(ctx);
      if(agent&&STATUS_GLOW[agent.status]){
        const on=frame.still||Math.floor(now/640)%2;
        ctx.save();ctx.globalCompositeOperation='screen';ctx.globalAlpha=on?.3:.18;ctx.fillStyle=STATUS_GLOW[agent.status];
        ctx.fillRect(438,348,72,40);ctx.restore();
      }
      if(frame.hover==='member'||frame.focus==='member'){
        ctx.strokeStyle=PALETTE.gold;ctx.lineWidth=2*frame.pixel;ctx.beginPath();ctx.ellipse(motion.x,motion.y-2,26,9,0,0,Math.PI*2);ctx.stroke();
      }
      if(motion.y<=motion.chairY)drawActor(ctx);
      drawOfficeChair(ctx,chairAtlas,CHAIR_X,motion.chairY,SPRITE_HEIGHT*.72);
      if(motion.y>motion.chairY)drawActor(ctx);
      frame.screen();
      if(!agent)return;
      const top=project({x:motion.x,y:motion.y-SPRITE_HEIGHT*(1-.22*motion.seatAmount)-2});
      const emote=agent.status==='running'?'work':agent.status==='pending'?'hourglass':'';
      if(emote)painter.emote(ctx,emote,top.x,top.y,now,frame.still);
      if(frame.hover==='member'||frame.focus==='member')painter.plate(ctx,ports.roleLabel(agent.role),top.x,top.y-(emote?30:4));
    },
    nextWake(now,motionless){
      if(motion.current&&!still)return 0;
      if(motionless||!agent)return Infinity;
      const period=agent.status==='running'?emoteFrameMs('work'):agent.status==='pending'?emoteFrameMs('hourglass'):0;
      return period?period-(now%period):Infinity;
    },
    hit(point){
      if(!agent)return '';
      const [x,y,w,h]=bodyRect();return point.x>=x&&point.x<=x+w&&point.y>=y&&point.y<=y+h?'member':'';
    },
    targets(){
      if(!agent)return [];
      const [x,y,width,height]=bodyRect();
      return [{id:'member',label:`${ports.roleLabel(agent.role)} · ${ports.statusLabel(agent.status)}`,x,y,width,height,
        data:{member:agent.id,status:agent.status,phase:motion.current?.kind||(motion.seated?'seated':'idle'),seated:String(motion.seated),beat}} as StageTarget];
    },
    activate(id){if(id==='member'&&agent)ports.openMember();},
    setStill(next){still=next;if(still&&motion.current){targetPodMotion(motion,motion.status,false);beat='';}},
  };
  room.onload=()=>{if(disposed)return;backdrop=makeBackdrop(room,room.naturalWidth,room.naturalHeight);ports.wake();};
  room.onerror=()=>{if(!disposed)ports.wake();};
  chairAtlas.onload=()=>{if(!disposed)ports.wake();};
  chairAtlas.src=chairArt;
  return {
    scene,
    update(member:PodAgent|null,visual:{theme:PodTheme;accent:number}){
      if(disposed)return;
      const next=member||undefined;
      if(agent?.id!==next?.id){motion=createPodMotion(next?.status||'idle');beat='';}
      else if(next&&agent&&agent.status!==next.status){
        targetPodMotion(motion,next.status,!still);
        beat=motion.current?(next.status==='running'?'arrive':'leave'):'';
      }
      agent=next;theme=visual.theme;accent=visual.accent;
      if(agent)characters.load(modelForRole(agent.role));
      const art=ROOM_ART[theme];
      if(requested!==art){requested=art;backdrop=undefined;room.src=art;}
    },
    dispose(){if(disposed)return;disposed=true;characters.dispose();room.onload=null;room.onerror=null;chairAtlas.onload=null;},
  };
}
export type PodScene = ReturnType<typeof createPodScene>;
