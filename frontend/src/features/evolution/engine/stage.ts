/** Game stage: one canvas, one fixed-step loop, one camera and one gesture
 * layer hosting the active scene. Owns device-pixel backing storage, resize,
 * hidden/offscreen sleep, inset transitions, camera follow, held movement keys,
 * an accessible button layer mirrored over scene targets, and teardown.
 * Entry: mountStage. Scenes own world state; the stage owns no task state.
 */
import { createGameLoop } from './loop';
import { createCamera, resizeCamera, setInsets, worldToScreen, screenToWorld, zoomAt, panBy,
  resetCamera, cameraScale, clampCamera, overflows, type Camera, type Insets, type Vec } from './camera';
import { attachPointer } from './pointer';
import { createPainter, type Painter } from './paint';

export type StageTarget = {id:string;label:string;x:number;y:number;width:number;height:number;
  pressed?:boolean;data?:Record<string,string>};
export interface SceneFrame {
  ctx:CanvasRenderingContext2D;camera:Camera;painter:Painter;now:number;
  /** World units per CSS pixel at the current zoom. */
  pixel:number;hover:string;focus:string;still:boolean;
  world():void;screen():void;project(point:Vec):Vec;
}
export interface Scene {
  readonly width:number;readonly height:number;
  backdrop():CanvasImageSource|undefined;
  step(dtMs:number,input:{axis:Vec}):void;
  draw(frame:SceneFrame):void;
  /** 0 = continuous motion; >0 = ms to the next visual change; Infinity = nothing scheduled. */
  nextWake(now:number,still:boolean):number;
  hit(point:Vec):string;
  targets():StageTarget[];
  activate(id:string,source:'pointer'|'keyboard'):void;
  tapFloor?(point:Vec):void;
  interact?():void;
  follow?():Vec|undefined;
  setStill?(still:boolean):void;
}
const MOVE_KEYS:Record<string,Vec> = {
  ArrowLeft:{x:-1,y:0},ArrowRight:{x:1,y:0},ArrowUp:{x:0,y:-1},ArrowDown:{x:0,y:1},
  KeyA:{x:-1,y:0},KeyD:{x:1,y:0},KeyW:{x:0,y:-1},KeyS:{x:0,y:1},
};
/** Inset and follow easing time constant (presentation). */
const EASE_MS = 140;

export function mountStage(host:HTMLElement,options:{label:string;fontFamily?:string}) {
  host.classList.add('evo-stage');host.tabIndex=0;host.setAttribute('aria-label',options.label);
  const canvas=document.createElement('canvas');canvas.className='evo-stage-canvas';canvas.setAttribute('aria-hidden','true');
  const layer=document.createElement('div');layer.className='evo-stage-targets';
  host.replaceChildren(canvas,layer);
  const ctx=canvas.getContext('2d');
  const painter=createPainter(options.fontFamily||window.getComputedStyle(host).fontFamily||'sans-serif');
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const camera=createCamera(960,720);
  let scene:Scene|undefined,disposed=false,intersecting=true,motionPaused=false,dpr=1;
  let hover='',focus='',dragging=false,userCamera=false,signature='';
  let insets:Insets={top:0,right:0,bottom:0,left:0},targetInsets:Insets={...insets};
  const held=new Map<string,Vec>();
  const buttons=new Map<string,HTMLButtonElement>();
  const metrics={paints:0,resizes:0};
  const still=()=>motionPaused||!!reduced?.matches;
  const visible=()=>!disposed&&!host.hidden&&!document.hidden&&intersecting;
  const loop=createGameLoop({
    visible,
    step(dt){
      if(!scene)return;
      easeInsets(dt);
      scene.step(dt,{axis:axis()});
      const goal=followGoal();
      if(goal){const k=still()?1:1-Math.exp(-dt/EASE_MS);camera.x+=(goal.x-camera.x)*k;camera.y+=(goal.y-camera.y)*k;clampCamera(camera);}
    },
    render(now){render(now);},
    nextWake(now){
      if(!scene)return Infinity;
      if(insetsMoving()||held.size||followGoal())return 0;
      return scene.nextWake(now,still());
    },
  });
  /** A world larger than its frame keeps the scene's focus in view until the
   * human pans; the goal is clamped, so an unreachable focus never spins. */
  function followGoal():Vec|undefined {
    if(!scene||dragging||userCamera||!overflows(camera))return undefined;
    const target=scene.follow?.();if(!target)return undefined;
    const probe=clampCamera({...camera,x:target.x,y:target.y});
    return Math.abs(probe.x-camera.x)+Math.abs(probe.y-camera.y)>.5?{x:probe.x,y:probe.y}:undefined;
  }
  function axis():Vec {
    let x=0,y=0;held.forEach(value=>{x+=value.x;y+=value.y;});
    const length=Math.hypot(x,y);return length?{x:x/length,y:y/length}:{x:0,y:0};
  }
  function insetsMoving(){return (Object.keys(insets) as Array<keyof Insets>).some(key=>Math.abs(insets[key]-targetInsets[key])>.5);}
  function easeInsets(dt:number){
    if(!insetsMoving())return;
    const k=still()?1:1-Math.exp(-dt/EASE_MS);
    for(const key of Object.keys(insets) as Array<keyof Insets>)insets[key]+=(targetInsets[key]-insets[key])*k;
    if(!insetsMoving())insets={...targetInsets};
    setInsets(camera,insets);
  }
  function resize(){
    if(disposed)return;
    const width=host.clientWidth||960,height=host.clientHeight||720;
    dpr=window.devicePixelRatio||1;
    const w=Math.round(width*dpr),h=Math.round(height*dpr);
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;metrics.resizes++;}
    resizeCamera(camera,width,height);signature='';loop.wake();
  }
  function frame(now:number):SceneFrame {
    const scale=cameraScale(camera),origin=worldToScreen(camera,{x:0,y:0});
    return {ctx:ctx!,camera,painter,now,pixel:1/scale,hover,focus,still:still(),
      world(){ctx!.setTransform(dpr*scale,0,0,dpr*scale,dpr*origin.x,dpr*origin.y);},
      screen(){ctx!.setTransform(dpr,0,0,dpr,0,0);},
      project:point=>worldToScreen(camera,point)};
  }
  function render(now:number){
    if(!scene)return;
    syncTargets();
    if(!ctx)return;
    metrics.paints++;
    ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.setTransform(dpr,0,0,dpr,0,0);
    const backdrop=scene.backdrop(),w=camera.viewWidth,h=camera.viewHeight;
    ctx.fillStyle='#1c140e';ctx.fillRect(0,0,w,h);
    if(backdrop){
      const bw=(backdrop as HTMLCanvasElement).width||1,bh=(backdrop as HTMLCanvasElement).height||1;
      const cover=Math.max(w/bw,h/bh);
      ctx.imageSmoothingEnabled=true;ctx.globalAlpha=.55;
      ctx.drawImage(backdrop,(w-bw*cover)/2,(h-bh*cover)/2,bw*cover,bh*cover);ctx.globalAlpha=1;
    }
    const vignette=ctx.createRadialGradient(w/2,h/2,Math.min(w,h)*.2,w/2,h/2,Math.max(w,h)*.75);
    vignette.addColorStop(0,'rgba(20,13,8,0.25)');vignette.addColorStop(1,'rgba(20,13,8,0.8)');
    ctx.fillStyle=vignette;ctx.fillRect(0,0,w,h);
    scene.draw(frame(now));
    ctx.setTransform(1,0,0,1,0,0);
  }
  function syncTargets(){
    if(!scene)return;
    const targets=scene.targets(),scale=cameraScale(camera),origin=worldToScreen(camera,{x:0,y:0});
    // Button geometry is origin + world × scale, so this signature covers every
    // camera, HUD-inset and viewport change that moves a target.
    const next=JSON.stringify([targets,origin.x,origin.y,scale]);
    if(next===signature)return;signature=next;
    const keep=new Set<string>();
    targets.forEach((target,index)=>{
      keep.add(target.id);
      let button=buttons.get(target.id);
      if(!button){
        button=document.createElement('button');button.type='button';button.className='evo-target';button.dataset.target=target.id;
        button.addEventListener('click',()=>{if(!disposed&&button!.dataset.target)scene?.activate(button!.dataset.target,'keyboard');});
        button.addEventListener('focus',()=>{focus=target.id;loop.wake();});
        button.addEventListener('blur',()=>{if(focus===target.id){focus='';loop.wake();}});
        buttons.set(target.id,button);
      }
      if(layer.children[index]!==button)layer.insertBefore(button,layer.children[index]||null);
      const corner=worldToScreen(camera,{x:target.x,y:target.y});
      button.style.left=`${corner.x}px`;button.style.top=`${corner.y}px`;
      button.style.width=`${target.width*scale}px`;button.style.height=`${target.height*scale}px`;
      if(button.getAttribute('aria-label')!==target.label)button.setAttribute('aria-label',target.label);
      if(target.pressed===undefined)button.removeAttribute('aria-pressed');else button.setAttribute('aria-pressed',String(target.pressed));
      for(const [key,value] of Object.entries(target.data||{}))if(button.dataset[key]!==value)button.dataset[key]=value;
    });
    for(const [id,button] of buttons)if(!keep.has(id)){button.remove();buttons.delete(id);}
  }
  const detachPointer=attachPointer(canvas,{
    hover(point){
      const id=point&&scene?scene.hit(screenToWorld(camera,point)):'';
      canvas.style.cursor=id?'pointer':camera.zoom>1?'grab':'default';
      if(id!==hover){hover=id;loop.wake();}
    },
    tap(point){
      if(!scene)return;
      host.focus({preventScroll:true});
      const world=screenToWorld(camera,point),id=scene.hit(world);
      if(id)scene.activate(id,'pointer');else scene.tapFloor?.(world);
      loop.wake();
    },
    pan(dx,dy){userCamera=true;panBy(camera,dx,dy);loop.wake();},
    zoom(factor,point){userCamera=!(factor<1&&camera.zoom*factor<=1);zoomAt(camera,factor,point);loop.wake();},
    dragging(active){dragging=active;canvas.dataset.dragging=String(active);},
  });
  function movementTarget(event:KeyboardEvent){
    const target=event.target as Element|null;
    return target===host||target===document.body||!!target?.closest?.('.evo-stage-targets');
  }
  function keydown(event:KeyboardEvent){
    if(event.altKey||event.ctrlKey||event.metaKey||!movementTarget(event))return;
    const move=MOVE_KEYS[event.code];
    if(move){event.preventDefault();if(!held.has(event.code)){held.set(event.code,move);userCamera=false;loop.wake();}return;}
    if(event.target===host&&(event.code==='KeyE'||event.code==='Enter'||event.code==='Space')){event.preventDefault();scene?.interact?.();loop.wake();return;}
    if(event.key==='+'||event.key==='='){zoomAt(camera,1.25,center());loop.wake();}
    else if(event.key==='-'){zoomAt(camera,.8,center());loop.wake();}
    else if(event.key==='0'){resetCamera(camera);userCamera=false;loop.wake();}
  }
  function keyup(event:KeyboardEvent){if(held.delete(event.code))loop.wake();}
  function center():Vec{return {x:camera.insets.left+(camera.viewWidth-camera.insets.left-camera.insets.right)/2,y:camera.insets.top+(camera.viewHeight-camera.insets.top-camera.insets.bottom)/2};}
  function blurWindow(){if(held.size){held.clear();loop.wake();}}
  function visibility(){if(visible()){signature='';loop.wake();}else{held.clear();loop.stop();}}
  function motionPreference(){scene?.setStill?.(still());loop.wake();}
  const resizeObserver=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):undefined;
  resizeObserver?.observe(host);
  const intersection=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{intersecting=entries[0]?.isIntersecting??false;visibility();}):undefined;
  intersection?.observe(host);
  document.addEventListener('visibilitychange',visibility);document.addEventListener('keydown',keydown);document.addEventListener('keyup',keyup);
  window.addEventListener('blur',blurWindow);reduced?.addEventListener('change',motionPreference);
  resize();
  return {
    camera,
    setScene(next:Scene){
      if(disposed)return;
      scene=next;camera.worldWidth=next.width;camera.worldHeight=next.height;resetCamera(camera);
      userCamera=false;hover='';focus='';signature='';
      buttons.forEach(button=>button.remove());buttons.clear();
      next.setStill?.(still());loop.wake();
    },
    setInsets(next:Insets){
      targetInsets={...next};
      if(still()||!scene){insets={...next};setInsets(camera,insets);}
      signature='';loop.wake();
    },
    setMotionPaused(paused:boolean){motionPaused=paused;host.dataset.motion=paused?'paused':'live';scene?.setStill?.(still());loop.wake();},
    still,
    wake(){loop.wake();},
    resetView(){resetCamera(camera);userCamera=false;loop.wake();},
    focusTarget(id:string){buttons.get(id)?.focus({preventScroll:true});},
    metrics(){return {...loop.metrics(),...metrics,pending:loop.pending(),backingPixels:canvas.width*canvas.height};},
    dispose(){
      if(disposed)return;disposed=true;loop.dispose();detachPointer();
      resizeObserver?.disconnect();intersection?.disconnect();
      document.removeEventListener('visibilitychange',visibility);document.removeEventListener('keydown',keydown);document.removeEventListener('keyup',keyup);
      window.removeEventListener('blur',blurWindow);reduced?.removeEventListener('change',motionPreference);
      buttons.clear();held.clear();canvas.width=canvas.height=0;host.replaceChildren();host.classList.remove('evo-stage');
    },
  };
}
export type Stage = ReturnType<typeof mountStage>;
