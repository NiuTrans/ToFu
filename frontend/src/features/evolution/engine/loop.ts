/** Fixed-step game loop over visible time. Simulation advances in exact
 * STEP_MS increments and every animation frame renders once. The loop asks the
 * world when it next changes: 0 keeps frames coming, a positive delay sleeps on
 * one timer until that keyframe, Infinity sleeps with no callbacks at all.
 * Hidden pages own nothing. Frame scheduler and timers are injectable.
 * Entry: createGameLoop. No DOM, assets or task state.
 */
export const STEP_MS = 1000 / 60;
/** Liveness bound: after a stalled or throttled frame the simulation never
 * replays more elapsed time than this in one burst. */
export const MAX_FRAME_MS = 250;
/** Floating-point tolerance: fractional frame remainders that sum to one step
 * (e.g. 2.5 + 0.5) must step instead of losing it to rounding. */
const STEP_EPSILON = 1e-9;

export interface GameLoopPorts {
  step(dtMs:number):void;
  render(nowMs:number):void;
  /** 0 = next frame; >0 = ms until the next scheduled visual change; Infinity = asleep. */
  nextWake(nowMs:number):number;
  visible():boolean;
  frames?:{request(callback:(now:number)=>void):number;cancel(id:number):void};
  timers?:{set(callback:()=>void,ms:number):number;clear(id:number):void};
}

export function createGameLoop(ports:GameLoopPorts) {
  const frames=ports.frames||{request:(callback:(now:number)=>void)=>window.requestAnimationFrame(callback),cancel:(id:number)=>window.cancelAnimationFrame(id)};
  const timers=ports.timers||{set:(callback:()=>void,ms:number)=>window.setTimeout(callback,ms),clear:(id:number)=>window.clearTimeout(id)};
  let frame=0,timer=0,accumulator=0,disposed=false;
  // Undefined means a fresh clock: the next frame renders without stepping.
  let last:number|undefined;
  const metrics={frames:0,steps:0,sleeps:0,timers:0};
  function clearTimer(){if(timer){timers.clear(timer);timer=0;}}
  function request(){if(!frame&&!disposed&&ports.visible())frame=frames.request(tick);}
  function settle(now:number){
    clearTimer();
    const wake=ports.nextWake(now);
    if(wake<=0){request();return;}
    // Sleeping worlds resume from a fresh clock: no motion was pending.
    last=undefined;accumulator=0;
    if(Number.isFinite(wake)){metrics.timers++;timer=timers.set(()=>{timer=0;request();},wake);}
    else metrics.sleeps++;
  }
  function tick(now:number){
    frame=0;
    if(disposed||!ports.visible()){last=undefined;accumulator=0;return;}
    if(last!==undefined){
      accumulator+=Math.min(Math.max(0,now-last),MAX_FRAME_MS);
      while(accumulator>=STEP_MS-STEP_EPSILON){ports.step(STEP_MS);accumulator=Math.max(0,accumulator-STEP_MS);metrics.steps++;}
    }
    last=now;
    ports.render(now);metrics.frames++;
    settle(now);
  }
  return {
    /** Render on the next frame and keep running while the world is busy. */
    wake(){if(disposed)return;clearTimer();request();},
    /** Release every callback; a later wake() starts from a fresh clock. */
    stop(){clearTimer();if(frame){frames.cancel(frame);frame=0;}last=undefined;accumulator=0;},
    pending(){return {frame:!!frame,timer:!!timer};},
    metrics(){return {...metrics};},
    dispose(){if(disposed)return;disposed=true;clearTimer();if(frame){frames.cancel(frame);frame=0;}},
  };
}
