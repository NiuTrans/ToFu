/** Lazy authored game-character painter for the main office and agent pods.
 * Every role owns a real four-direction walking sheet. The earlier atlas is
 * only a network-failure fallback; no vector costume is composited over it.
 * Source art is under assets/game-*.webp; crop data is generated from alpha.
 */
import executor from './assets/game-executor.webp';
import reviewer from './assets/game-reviewer.webp';
import coordinator from './assets/game-coordinator.webp';
import researcher from './assets/game-researcher.webp';
import engineer from './assets/game-engineer.webp';
import writer from './assets/game-writer.webp';
import analyst from './assets/game-analyst.webp';
import general from './assets/game-general.webp';
import people from './assets/people.png';
import { FRAMES } from './office-sprites';
import { authoredFrame, spriteGeometry } from './office-sprite-geometry';
import { GAME_FRAMES } from './office-game-frames';
import type { CharacterModel } from './office-character';

const ART:Record<CharacterModel,string>={executor,reviewer,coordinator,researcher,engineer,writer,analyst,general};

export function createCharacterPainter(onReady:()=>void){
  const loaded=new Map<CharacterModel,HTMLImageElement>();
  const fallback=new window.Image();
  let disposed=false;
  fallback.onload=()=>{if(!disposed)onReady();};
  function load(model:CharacterModel){
    if(loaded.has(model)||disposed)return;
    const image=new window.Image();loaded.set(model,image);
    image.onload=()=>{if(!disposed)onReady();};
    image.onerror=()=>{
      if(disposed)return;
      if(!fallback.src)fallback.src=people;
      onReady();
    };
    image.src=ART[model];
  }
  function readyFor(models:CharacterModel[]){
    return models.every(model=>!!loaded.get(model)?.naturalWidth||!!fallback.naturalWidth);
  }
  function draw(ctx:CanvasRenderingContext2D,model:CharacterModel,sprite:number,
    direction:number,frame:number,x:number,y:number,height:number,seated:boolean|number){

    if(disposed)return;
    load(model);
    const authored=loaded.get(model);
    const ready=!!authored?.complete&&!!authored.naturalWidth;
    const atlas=ready?authored:fallback;
    if(!atlas.complete||!atlas.naturalWidth)return;
    const {row,column}=authoredFrame(direction,frame);
    const bounds=ready?GAME_FRAMES[model][row][column]:FRAMES[sprite*4+direction][frame];
    const referenceHeight=ready?GAME_FRAMES[model][0][1][3]:FRAMES[sprite*4][1][3];
    const box=spriteGeometry(bounds,referenceHeight,x,y,height,Number(seated));
    ctx.drawImage(atlas,box.sx,box.sy,box.sw,box.sh,box.x,box.y,box.width,box.height);
  }
  return {load,draw,readyFor,dispose(){disposed=true;loaded.forEach(image=>{image.onload=null;image.onerror=null;});loaded.clear();fallback.onload=null;fallback.onerror=null;}};
}
