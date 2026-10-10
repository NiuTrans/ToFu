/** Bounded four-neighbour pathfinding in office artwork coordinates (960 × 720).
 * Furniture footprints include clearance for feet. No task or network state.
 * Endpoints join the grid with checked orthogonal segments, never corner cuts.
 */
export type Point = {x: number;y: number};
export const FLOOR_WIDTH = 960;
export const FLOOR_HEIGHT = 720;
export const GRID = 20;
const COLS = FLOOR_WIDTH / GRID;
const ROWS = FLOOR_HEIGHT / GRID;
const FURNITURE = [
  [0,0,960,115], [70,115,330,255], [620,115,865,245],
  [0,250,145,395], [0,395,345,645], [815,265,960,645],
  [630,500,825,645], [340,555,410,645], [550,555,635,645],
];
export function walkable(point: Point): boolean {
  return Number.isFinite(point.x)&&Number.isFinite(point.y)&&
    point.x>=50 && point.x<=900 && point.y>=130 && point.y<=620 &&
    !FURNITURE.some(([left,top,right,bottom])=>point.x>=left&&point.x<=right&&point.y>=top&&point.y<=bottom);
}
export function walkableSegment(a:Point,b:Point):boolean {
  if(!walkable(a)||!walkable(b)||(a.x!==b.x&&a.y!==b.y))return false;
  return !FURNITURE.some(([left,top,right,bottom])=>
    Math.max(a.x,b.x)>=left&&Math.min(a.x,b.x)<=right&&
    Math.max(a.y,b.y)>=top&&Math.min(a.y,b.y)<=bottom);
}
function center(index:number):Point{return {x:(index%COLS)*GRID+GRID/2,y:Math.floor(index/COLS)*GRID+GRID/2};}
function cell(point:Point):number{return Math.floor(point.y/GRID)*COLS+Math.floor(point.x/GRID);}
const OPEN=Uint8Array.from({length:COLS*ROWS},(_,i)=>Number(walkable(center(i))));
function join(a:Point,b:Point):Point[]|undefined {
  if(a.x===b.x&&a.y===b.y)return [];
  if(walkableSegment(a,b))return [{...b}];
  for(const elbow of [{x:a.x,y:b.y},{x:b.x,y:a.y}]){
    if(walkableSegment(a,elbow)&&walkableSegment(elbow,b))return [elbow,{...b}];
  }
}
function anchor(point:Point):number|undefined {
  const col=Math.floor(point.x/GRID),row=Math.floor(point.y/GRID),candidates:number[]=[];
  for(let y=row-1;y<=row+1;y++)for(let x=col-1;x<=col+1;x++){
    const index=y*COLS+x;
    if(x>=0&&x<COLS&&y>=0&&y<ROWS&&OPEN[index]&&join(point,center(index)))candidates.push(index);
  }
  return candidates.sort((a,b)=>{
    const pa=center(a),pb=center(b);
    return Math.abs(pa.x-point.x)+Math.abs(pa.y-point.y)-Math.abs(pb.x-point.x)-Math.abs(pb.y-point.y);
  })[0];
}
export function findOfficePath(from:Point,to:Point):Point[] {
  if(!walkable(from)||!walkable(to))return [];
  if(cell(from)===cell(to)){const direct=join(from,to);if(direct)return direct;}
  const start=anchor(from),goal=anchor(to);
  if(start===undefined||goal===undefined)return [];
  const queue=[start],previous=new Int32Array(COLS*ROWS).fill(-2);previous[start]=-1;
  for(let head=0;head<queue.length&&previous[goal]===-2;head++){
    const current=queue[head];
    for(const next of [current-COLS,current+COLS,current-1,current+1]){
      if(next<0||next>=COLS*ROWS||previous[next]!==-2||!OPEN[next])continue;
      if(!walkableSegment(center(current),center(next)))continue;
      previous[next]=current;queue.push(next);
    }
  }
  if(previous[goal]===-2)return [];
  const path:Point[]=[];
  for(let at=goal;at!==start;at=previous[at])path.push(center(at));
  return [...join(from,center(start))!,...path.reverse(),...join(center(goal),to)!];
}
