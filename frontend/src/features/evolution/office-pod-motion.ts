/** Selected-room actor kinematics, independent of Canvas/DOM/assets.
 * Status changes are presentation targets, not inferred task progress. A new
 * target retains the current foot/chair/seat pose, including mid-action reversals.
 */
import type { Point } from './office-navigation';
import { stepWalker, seatPose, SEAT_TRANSITION_MS, TURN_MS, type Motion } from './office-motion';
const SEAT={x:480,y:478},DOOR={x:826,y:430},DONE={x:675,y:610};
const CHAIR_IN=502,CHAIR_OUT=548,CHAIR_MS=400;
type Action={kind:'walk';to:Point}|{kind:'face';direction:number}|{kind:'seat';amount:number}|{kind:'chair';y:number;attached:boolean};
export type PodMotion=Motion & {seatAmount:number;seated:boolean;chairY:number;queue:Action[];current?:Action;elapsed:number;from:number;status:string};
export function createPodMotion(status:string):PodMotion {
  const seated=status==='running';
  return {...(seated?SEAT:status==='pending'?DOOR:DONE),path:[],direction:seated?3:status==='pending'?1:0,
    frame:1,strideDistance:0,seatAmount:Number(seated),seated,chairY:CHAIR_IN,queue:[],elapsed:0,from:0,status};
}
function next(motion:PodMotion){
  motion.current=motion.queue.shift();motion.elapsed=0;
  const action=motion.current;
  if(!action)return;
  if(action.kind==='walk')motion.path=[action.to];
  if(action.kind==='face'){motion.direction=action.direction;motion.frame=1;}
  if(action.kind==='seat')motion.from=motion.seatAmount;
  if(action.kind==='chair')motion.from=motion.chairY;
}
export function targetPodMotion(motion:PodMotion,status:string,animate=true){
  if(!animate){Object.assign(motion,createPodMotion(status),{current:undefined});return;}
  motion.status=status;motion.queue=[];motion.path=[];motion.current=undefined;motion.frame=1;
  const queue=motion.queue;
  if(motion.seated||motion.seatAmount>0){
    queue.push({kind:'chair',y:CHAIR_OUT,attached:true},{kind:'seat',amount:0});
    queue.push({kind:'walk',to:{x:528,y:524}},{kind:'chair',y:CHAIR_IN,attached:false});
  }else if(motion.chairY!==CHAIR_IN){queue.push({kind:'chair',y:CHAIR_IN,attached:false});}
  // Orthogonal approach stays below the desk footprint, and finishes at the
  // exact foot anchor before changing posture. No timed lerp through furniture.
  queue.push({kind:'walk',to:{x:motion.seated||motion.seatAmount>0?528:motion.x,y:630}},
    {kind:'walk',to:{x:528,y:630}});
  if(status==='running'){
    queue.push({kind:'walk',to:{x:528,y:562}},{kind:'chair',y:CHAIR_OUT,attached:false},
      {kind:'walk',to:{x:480,y:562}},{kind:'walk',to:{x:480,y:524}},
      {kind:'face',direction:3},{kind:'seat',amount:1},{kind:'chair',y:CHAIR_IN,attached:true});
  }else{
    const target=status==='pending'?DOOR:DONE;
    queue.push({kind:'walk',to:{x:target.x,y:630}},{kind:'walk',to:target},{kind:'face',direction:status==='pending'?1:0});
  }
  next(motion);
}
export function stepPodMotion(motion:PodMotion,dtMs:number){
  const action=motion.current;if(!action)return;
  motion.elapsed+=Math.max(0,dtMs);
  let done=false;
  if(action.kind==='walk')done=stepWalker(motion,dtMs);
  else if(action.kind==='face')done=motion.elapsed>=TURN_MS;
  else if(action.kind==='seat'){
    motion.seatAmount=seatPose(motion.from,action.amount,motion.elapsed);
    done=motion.elapsed>=SEAT_TRANSITION_MS;
    if(done)motion.seated=action.amount===1;
  }else{
    const t=Math.min(1,motion.elapsed/CHAIR_MS),eased=t*t*(3-2*t);
    motion.chairY=motion.from+(action.y-motion.from)*eased;
    if(action.attached){motion.x=480;motion.y=motion.chairY-24;}
    done=t===1;
  }
  if(done)next(motion);
}
