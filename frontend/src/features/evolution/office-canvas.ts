/** Small Canvas2D surface seam. No ticker, asset ownership, WebGL context or
 * task state. A signature suppresses unchanged paints; image decode/resize
 * explicitly invalidates it. Counters are constant-space diagnostics.
 */
import { FLOOR_WIDTH, FLOOR_HEIGHT } from './office-navigation';
export function createOfficeCanvasSurface(canvas:HTMLCanvasElement){
  const ctx=canvas.getContext('2d');
  let disposed=false,signature:string|undefined;
  const metrics={paintRequests:0,paints:0,skippedPaints:0,resizes:0,backingPixels:0};
  return {
    ctx,
    resize(width:number,dpr:number){
      if(disposed)return false;
      const scale=Math.min(Math.max(1,width)/FLOOR_WIDTH*Math.min(2,Math.max(1,dpr)),1.5);
      const w=Math.round(FLOOR_WIDTH*scale),h=Math.round(FLOOR_HEIGHT*scale);
      if(canvas.width===w&&canvas.height===h)return false;
      canvas.width=w;canvas.height=h;metrics.resizes++;metrics.backingPixels=w*h;
      ctx?.setTransform(scale,0,0,scale,0,0);if(ctx)ctx.imageSmoothingEnabled=false;
      signature=undefined;return true;
    },
    invalidate(){signature=undefined;},
    paint(next:string,draw:()=>void){
      if(disposed)return false;
      metrics.paintRequests++;
      if(next===signature){metrics.skippedPaints++;return false;}
      signature=next;
      if(ctx){ctx.clearRect(0,0,FLOOR_WIDTH,FLOOR_HEIGHT);draw();metrics.paints++;}
      return true;
    },
    metrics(){return {...metrics};},
    dispose(){if(disposed)return;disposed=true;signature=undefined;canvas.width=canvas.height=0;metrics.backingPixels=0;},
  };
}
