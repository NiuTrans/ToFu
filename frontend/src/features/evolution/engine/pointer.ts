/** Pointer gestures on one element, in CSS pixels relative to its box:
 * mouse hover, tap, drag-pan, wheel zoom and two-finger pinch. Callbacks only;
 * camera math and hit testing belong to the stage. Entry: attachPointer.
 */
import type { Vec } from './camera';

/** Movement under which a press still counts as a tap (CSS px). */
export const TAP_SLOP_PX = 6;
/** One standard 100px wheel notch zooms by 20%. */
const WHEEL_ZOOM_PER_PX = Math.log(1.2) / 100;

export interface PointerHandlers {
  hover(point:Vec|null):void;
  tap(point:Vec,event:PointerEvent):void;
  pan(dx:number,dy:number):void;
  zoom(factor:number,point:Vec):void;
  dragging?(active:boolean):void;
}

export function attachPointer(element:HTMLElement,handlers:PointerHandlers):()=>void {
  const pointers=new Map<number,Vec>();
  let start:Vec|undefined,dragging=false,pinch=0;
  const local=(event:{clientX:number;clientY:number}):Vec=>{
    const rect=element.getBoundingClientRect();return {x:event.clientX-rect.left,y:event.clientY-rect.top};
  };
  const spread=()=>{const [a,b]=[...pointers.values()];return a&&b?Math.hypot(a.x-b.x,a.y-b.y):0;};
  const middle=():Vec=>{const [a,b]=[...pointers.values()];return {x:(a.x+b.x)/2,y:(a.y+b.y)/2};};
  function setDragging(next:boolean){if(dragging!==next){dragging=next;handlers.dragging?.(next);}}
  function down(event:PointerEvent){
    if(event.pointerType==='mouse'&&event.button!==0)return;
    element.setPointerCapture?.(event.pointerId);
    pointers.set(event.pointerId,local(event));
    if(pointers.size===1){start=local(event);setDragging(false);}
    else{start=undefined;setDragging(true);pinch=spread();}
  }
  function move(event:PointerEvent){
    const point=local(event),previous=pointers.get(event.pointerId);
    if(!previous){if(event.pointerType==='mouse')handlers.hover(point);return;}
    pointers.set(event.pointerId,point);
    if(pointers.size>=2){
      const distance=spread();
      if(pinch>0&&distance>0)handlers.zoom(distance/pinch,middle());
      pinch=distance;return;
    }
    if(!dragging&&start&&Math.hypot(point.x-start.x,point.y-start.y)>TAP_SLOP_PX)setDragging(true);
    if(dragging)handlers.pan(point.x-previous.x,point.y-previous.y);
  }
  function up(event:PointerEvent){
    if(!pointers.delete(event.pointerId))return;
    if(pointers.size){pinch=spread();return;}
    if(!dragging&&start)handlers.tap(local(event),event);
    setDragging(false);start=undefined;pinch=0;
  }
  function cancel(event:PointerEvent){
    pointers.delete(event.pointerId);
    if(!pointers.size){setDragging(false);start=undefined;pinch=0;}
  }
  function leave(event:PointerEvent){if(event.pointerType==='mouse'&&!pointers.size)handlers.hover(null);}
  function wheel(event:WheelEvent){
    event.preventDefault();
    const pixels=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?element.clientHeight:1);
    handlers.zoom(Math.exp(-pixels*WHEEL_ZOOM_PER_PX),local(event));
  }
  element.addEventListener('pointerdown',down);element.addEventListener('pointermove',move);
  element.addEventListener('pointerup',up);element.addEventListener('pointercancel',cancel);
  element.addEventListener('pointerleave',leave);element.addEventListener('wheel',wheel,{passive:false});
  return ()=>{
    element.removeEventListener('pointerdown',down);element.removeEventListener('pointermove',move);
    element.removeEventListener('pointerup',up);element.removeEventListener('pointercancel',cancel);
    element.removeEventListener('pointerleave',leave);element.removeEventListener('wheel',wheel);
    pointers.clear();
  };
}
