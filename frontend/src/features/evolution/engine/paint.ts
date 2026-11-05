/** Canvas painter for in-world game UI: shadows, hotspot brackets, name
 * plates, speech bubbles, animated emotes, move targets, the player marker and
 * the blurred backdrop. World-space helpers draw under the camera transform;
 * plate/bubble/emote/marker draw in screen pixels at a projected point so text
 * stays crisp at every zoom. Pure drawing: no DOM ownership, timers or state.
 * Entry: createPainter, emoteFrameMs, makeBackdrop.
 */
import type { Emote } from '../studio/projection';

export const PALETTE = {
  ink:'#241a12', panel:'rgba(33,24,17,0.9)', panelEdge:'rgba(240,207,122,0.75)',
  gold:'#f0cf7a', goldDeep:'#c99a3b', cream:'#fbf1da', paper:'#fff8e8',
  good:'#7fd18b', warn:'#f2bf55', bad:'#ee7462', info:'#8ec2f2', shadow:'rgba(30,20,12,0.28)',
};
type Ctx = CanvasRenderingContext2D;
/** Visual period of each emote's animation frame; the loop sleeps until the next one. */
const EMOTE_FRAME_MS:Record<Exclude<Emote,''>,number> = {
  work:320, think:420, ask:480, compute:160, hourglass:900, alert:420, fail:0,
};
export function emoteFrameMs(kind:Emote):number {return kind?EMOTE_FRAME_MS[kind]:0;}

function roundRect(ctx:Ctx,x:number,y:number,w:number,h:number,r:number){
  const radius=Math.max(0,Math.min(r,w/2,h/2));
  ctx.beginPath();ctx.moveTo(x+radius,y);ctx.arcTo(x+w,y,x+w,y+h,radius);ctx.arcTo(x+w,y+h,x,y+h,radius);
  ctx.arcTo(x,y+h,x,y,radius);ctx.arcTo(x,y,x+w,y,radius);ctx.closePath();
}

export function createPainter(fontFamily:string) {
  const font=(size:number,weight=600)=>`${weight} ${size}px ${fontFamily}`;
  function clip(ctx:Ctx,text:string,maxWidth:number){
    if(ctx.measureText(text).width<=maxWidth)return text;
    let value=text;
    while(value.length>1&&ctx.measureText(value+'…').width>maxWidth)value=value.slice(0,-1);
    return value+'…';
  }
  return {
    shadow(ctx:Ctx,x:number,y:number,rx:number,ry:number,alpha=1){
      ctx.save();ctx.globalAlpha*=alpha;ctx.fillStyle=PALETTE.shadow;
      ctx.beginPath();ctx.ellipse(x,y,rx,ry,0,0,Math.PI*2);ctx.fill();ctx.restore();
    },
    /** Selection brackets and a soft wash over a background-baked furniture rect (world units). */
    hotspot(ctx:Ctx,rect:readonly number[],pixel:number,now:number,tone:'hover'|'attention'|'focus'){
      const [x,y,w,h]=rect,pulse=tone==='attention'?.55+.45*Math.sin(now/260):1;
      const color=tone==='attention'?PALETTE.warn:PALETTE.gold;
      ctx.save();
      ctx.globalAlpha=(tone==='hover'?.16:.12)*pulse;ctx.fillStyle=color;roundRect(ctx,x,y,w,h,8*pixel);ctx.fill();
      ctx.globalAlpha=.95*pulse;ctx.strokeStyle=color;ctx.lineWidth=2.5*pixel;ctx.lineCap='round';
      const arm=Math.min(w,h)*.28;
      for(const [cx,cy,dx,dy] of [[x,y,1,1],[x+w,y,-1,1],[x,y+h,1,-1],[x+w,y+h,-1,-1]]){
        ctx.beginPath();ctx.moveTo(cx+dx*arm,cy);ctx.lineTo(cx,cy);ctx.lineTo(cx,cy+dy*arm);ctx.stroke();
      }
      ctx.restore();
    },
    /** Pulsing ring where a walk was requested (world units). */
    moveTarget(ctx:Ctx,x:number,y:number,pixel:number,age:number){
      const t=Math.min(1,age/700);
      ctx.save();ctx.globalAlpha=1-t;ctx.strokeStyle=PALETTE.gold;ctx.lineWidth=2*pixel;
      ctx.beginPath();ctx.ellipse(x,y,(10+10*t)*pixel*2,(4+4*t)*pixel*2,0,0,Math.PI*2);ctx.stroke();ctx.restore();
    },
    /** Name plate centered on x with its bottom edge at y (screen px). */
    plate(ctx:Ctx,text:string,x:number,y:number,tone:'default'|'self'|'muted'='default'){
      ctx.save();ctx.font=font(12);ctx.textBaseline='middle';ctx.textAlign='center';
      const label=clip(ctx,text,150),w=Math.ceil(ctx.measureText(label).width)+16,h=20;
      roundRect(ctx,Math.round(x-w/2)+.5,Math.round(y-h)+.5,w,h,6);
      ctx.fillStyle=tone==='self'?'rgba(122,74,24,0.94)':PALETTE.panel;ctx.fill();
      ctx.strokeStyle=tone==='muted'?'rgba(251,241,218,0.25)':PALETTE.panelEdge;ctx.lineWidth=1;ctx.stroke();
      ctx.fillStyle=tone==='muted'?'rgba(251,241,218,0.7)':PALETTE.cream;ctx.fillText(label,Math.round(x),Math.round(y-h/2)+1);
      ctx.restore();
    },
    /** Speech bubble whose tail tip is at (x, y) (screen px). */
    speech(ctx:Ctx,text:string,x:number,y:number){
      ctx.save();ctx.font=font(12,600);ctx.textBaseline='middle';ctx.textAlign='left';
      const label=clip(ctx,text,180),w=Math.ceil(ctx.measureText(label).width)+30,h=24,left=Math.round(x-w/2),top=Math.round(y-h-7);
      roundRect(ctx,left+.5,top+.5,w,h,8);
      ctx.fillStyle=PALETTE.paper;ctx.fill();ctx.strokeStyle=PALETTE.ink;ctx.lineWidth=1.5;ctx.stroke();
      ctx.beginPath();ctx.moveTo(x-5,top+h);ctx.lineTo(x,y);ctx.lineTo(x+5,top+h);ctx.closePath();ctx.fill();
      ctx.beginPath();ctx.moveTo(x-5,top+h+.5);ctx.lineTo(x,y);ctx.lineTo(x+5,top+h+.5);ctx.stroke();
      // Tool glyph: a small wrench made of two strokes.
      ctx.strokeStyle=PALETTE.goldDeep;ctx.lineWidth=2;ctx.lineCap='round';
      ctx.beginPath();ctx.moveTo(left+9,top+h-7);ctx.lineTo(left+15,top+7);ctx.stroke();
      ctx.beginPath();ctx.arc(left+16,top+7,3,0,Math.PI*2);ctx.stroke();
      ctx.fillStyle=PALETTE.ink;ctx.fillText(label,left+22,top+h/2+1);
      ctx.restore();
    },
    /** Animated status emote above a head; (x, y) is the badge's bottom tip (screen px). */
    emote(ctx:Ctx,kind:Emote,x:number,y:number,now:number,still:boolean){
      if(!kind)return;
      const period=EMOTE_FRAME_MS[kind],frame=still||!period?0:Math.floor(now/period);
      const bob=kind==='ask'||kind==='alert'?(frame%2?-2:0):0;
      const r=11,cx=Math.round(x),cy=Math.round(y-r-6+bob);
      const fill={work:PALETTE.paper,think:PALETTE.paper,ask:PALETTE.gold,compute:'#cfe6fb',hourglass:'#fde7b0',alert:PALETTE.bad,fail:'#d9c7c0'}[kind];
      ctx.save();
      ctx.fillStyle=fill;ctx.strokeStyle=PALETTE.ink;ctx.lineWidth=1.5;
      if(kind==='think'){
        ctx.beginPath();ctx.arc(cx-5,cy+r+3,2.4,0,Math.PI*2);ctx.fill();ctx.stroke();
      }else{
        ctx.beginPath();ctx.moveTo(cx-4,cy+r-2);ctx.lineTo(cx,cy+r+5);ctx.lineTo(cx+4,cy+r-2);ctx.closePath();ctx.fill();ctx.stroke();
      }
      ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fill();ctx.stroke();
      ctx.fillStyle=PALETTE.ink;ctx.strokeStyle=PALETTE.ink;ctx.lineCap='round';ctx.lineJoin='round';
      if(kind==='work'||kind==='think'){
        for(let i=0;i<3;i++){ctx.globalAlpha=still||frame%3===i?1:.32;ctx.beginPath();ctx.arc(cx-5+i*5,cy,1.8,0,Math.PI*2);ctx.fill();}
      }else if(kind==='ask'||kind==='alert'){
        ctx.fillStyle=kind==='alert'?'#fff':PALETTE.ink;ctx.font=font(15,800);ctx.textAlign='center';ctx.textBaseline='middle';
        ctx.fillText(kind==='ask'?'?':'!',cx,cy+1);
      }else if(kind==='fail'){
        ctx.lineWidth=2.4;ctx.beginPath();ctx.moveTo(cx-4,cy-4);ctx.lineTo(cx+4,cy+4);ctx.moveTo(cx+4,cy-4);ctx.lineTo(cx-4,cy+4);ctx.stroke();
      }else if(kind==='hourglass'){
        const flip=frame%2?-1:1;ctx.lineWidth=1.6;
        ctx.beginPath();ctx.moveTo(cx-4,cy-5*flip);ctx.lineTo(cx+4,cy-5*flip);ctx.lineTo(cx-4,cy+5*flip);ctx.lineTo(cx+4,cy+5*flip);ctx.closePath();ctx.stroke();
        ctx.beginPath();ctx.moveTo(cx-2,cy+4*flip);ctx.lineTo(cx+2,cy+4*flip);ctx.lineTo(cx,cy+1*flip);ctx.closePath();ctx.fill();
      }else if(kind==='compute'){
        // Server glyph with a turning activity arc.
        ctx.lineWidth=1.4;ctx.strokeRect(cx-4.5,cy-5,9,4);ctx.strokeRect(cx-4.5,cy+1,9,4);
        ctx.beginPath();ctx.arc(cx+2.5,cy-3,.9,0,Math.PI*2);ctx.arc(cx+2.5,cy+3,.9,0,Math.PI*2);ctx.fill();
        const a=still?0:(frame%8)*Math.PI/4;ctx.strokeStyle='#2f78c4';ctx.lineWidth=2;
        ctx.beginPath();ctx.arc(cx,cy,r+3,a,a+Math.PI*.55);ctx.stroke();
      }
      ctx.restore();
    },
    /** The player's marker above the head (screen px). Static: an idle world sleeps. */
    marker(ctx:Ctx,x:number,y:number){
      ctx.save();ctx.translate(Math.round(x),Math.round(y-6));
      ctx.fillStyle=PALETTE.gold;ctx.strokeStyle=PALETTE.ink;ctx.lineWidth=1.5;
      ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(-6,-8);ctx.lineTo(0,-12);ctx.lineTo(6,-8);ctx.closePath();ctx.fill();ctx.stroke();
      ctx.restore();
    },
    /** Interaction prompt key cap (screen px). */
    keycap(ctx:Ctx,label:string,x:number,y:number){
      ctx.save();ctx.font=font(11,800);ctx.textAlign='center';ctx.textBaseline='middle';
      roundRect(ctx,Math.round(x-10)+.5,Math.round(y-20)+.5,20,18,4);
      ctx.fillStyle=PALETTE.cream;ctx.fill();ctx.strokeStyle=PALETTE.ink;ctx.lineWidth=1.5;ctx.stroke();
      ctx.fillStyle=PALETTE.ink;ctx.fillText(label,Math.round(x),Math.round(y-11)+1);ctx.restore();
    },
  };
}
export type Painter = ReturnType<typeof createPainter>;

/** A tiny downsample of the room; drawn cover-sized with smoothing it reads as
 * a soft blur of the same scene behind the HUD. */
export function makeBackdrop(image:CanvasImageSource,width:number,height:number):HTMLCanvasElement|undefined {
  const canvas=document.createElement('canvas');canvas.width=48;canvas.height=Math.max(1,Math.round(48*height/width));
  const ctx=canvas.getContext('2d');if(!ctx)return undefined;
  ctx.imageSmoothingEnabled=true;ctx.drawImage(image,0,0,canvas.width,canvas.height);
  return canvas;
}
