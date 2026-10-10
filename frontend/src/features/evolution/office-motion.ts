/** Pure scene kinematics. Coordinates are foot anchors in the 960×720 world;
 * render cadence never owns gait phase. No DOM, clocks, task state or assets.
 */
import type { Point } from './office-navigation';
export const WALK_SPEED = 95;
export const STRIDE_DISTANCE = 52;
export const WALK_FRAMES = [1, 0, 3, 2] as const;
export const SEAT_TRANSITION_MS = 260;
export const TURN_MS = 100;
export type Motion = Point & {path:Point[];direction:number;frame:number;strideDistance:number};
export function facing(dx:number,dy:number):number {
  return Math.abs(dx)>Math.abs(dy)?(dx<0?1:2):(dy<0?3:0);
}
/** Consume the full distance budget, including leftover distance at corners.
 * Arrival continuations belong to the caller and run once after this function.
 */
export function stepWalker(walker:Motion,dtMs:number,speed=WALK_SPEED):boolean {
  let budget=Math.max(0,Number.isFinite(dtMs)?dtMs:0)*speed/1000;
  if(!(budget>0)||!Number.isFinite(budget))return false;
  const hadPath=walker.path.length>0;
  let travelled=0;
  while(walker.path.length&&budget>0){
    const next=walker.path[0],dx=next.x-walker.x,dy=next.y-walker.y;
    const distance=Math.hypot(dx,dy);
    if(distance<1e-8){walker.path.shift();continue;}
    walker.direction=facing(dx,dy);
    const step=Math.min(distance,budget);
    walker.x+=dx/distance*step;walker.y+=dy/distance*step;
    budget-=step;travelled+=step;
    if(step===distance){walker.x=next.x;walker.y=next.y;walker.path.shift();}
  }
  walker.strideDistance=(walker.strideDistance+travelled)%STRIDE_DISTANCE;
  walker.frame=walker.path.length&&travelled>0?
    WALK_FRAMES[Math.floor(walker.strideDistance/(STRIDE_DISTANCE/4))]:1;
  return hadPath&&!walker.path.length;
}
export function seatPose(from:number,to:number,elapsedMs:number):number {
  const t=Math.max(0,Math.min(1,elapsedMs/SEAT_TRANSITION_MS));
  return from+(to-from)*t*t*(3-2*t);
}
/** Chairs are rear-facing artwork. Their backrest must occlude the sitter,
 * rather than drawing a severed torso over the back of the chair.
 */
export const SEATED_LOWERING = .10;
