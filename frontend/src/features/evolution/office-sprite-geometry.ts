/** Runtime interpretation of authored sheets; alpha-bound tables remain generated.
 * All eight original sheets were inspected: side-row column 2 faces the OTHER
 * way. Swap its source row, not the whole sprite (costumes are asymmetric).
 */
import { SEAT_CROP } from './office-sprites';
import { SEATED_LOWERING } from './office-motion';
export function authoredFrame(direction:number,frame:number):{row:number;column:number} {
  const row=Math.max(0,Math.min(3,Math.trunc(direction)));
  const column=Math.max(0,Math.min(3,Math.trunc(frame)));
  return {row:column===2&&(row===1||row===2)?3-row:row,column};
}
export function spriteGeometry(bounds:readonly number[],referenceHeight:number,x:number,y:number,height:number,seat=0){
  const [sx,sy,sw,sh]=bounds,amount=Math.max(0,Math.min(1,seat));
  // Fixed model scale: alpha-height differences must not squash the torso on
  // every step. The world position always means the same planted foot anchor.
  const scale=height/referenceHeight;
  const cropHeight=sh*(1-(1-SEAT_CROP)*amount);
  return {sx,sy,sw,sh:cropHeight,x:x-sw*scale/2,
    y:y-sh*scale+height*SEATED_LOWERING*amount,width:sw*scale,height:cropHeight*scale};
}
