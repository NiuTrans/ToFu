/** World↔screen camera for one fixed-size world inside a resizable viewport.
 * The frame is the viewport minus HUD insets. Zoom 1 fits the world's height
 * to the frame, as a room game does: wide frames show the whole room with side
 * margins, narrow (portrait) frames crop horizontally and pan. Every axis the
 * world overflows stays covered. Pure state and math: no DOM, clocks or assets.
 * Entries: createCamera, resizeCamera, setInsets, worldToScreen, screenToWorld,
 * zoomAt, panBy, centerOn, resetCamera, cameraScale.
 */
export type Vec = {x:number;y:number};
export type Insets = {top:number;right:number;bottom:number;left:number};
export interface Camera {
  worldWidth:number;worldHeight:number;
  viewWidth:number;viewHeight:number;
  insets:Insets;
  zoom:number;
  /** World point at the frame center. */
  x:number;y:number;
}
/** Interaction bound for illustrated art: beyond this one art pixel blurs
 * across several screen pixels without revealing detail. */
export const MAX_ZOOM = 3;
const NO_INSETS:Insets = {top:0,right:0,bottom:0,left:0};

export function createCamera(worldWidth:number,worldHeight:number):Camera {
  return {worldWidth,worldHeight,viewWidth:worldWidth,viewHeight:worldHeight,insets:{...NO_INSETS},zoom:1,x:worldWidth/2,y:worldHeight/2};
}
function frame(camera:Camera){
  const {insets}=camera;
  const width=Math.max(1,camera.viewWidth-insets.left-insets.right);
  const height=Math.max(1,camera.viewHeight-insets.top-insets.bottom);
  return {width,height,cx:insets.left+width/2,cy:insets.top+height/2};
}
export function fitScale(camera:Camera):number {
  return frame(camera).height/camera.worldHeight;
}
/** True when the scaled world is larger than the frame on some axis. */
export function overflows(camera:Camera):boolean {
  const scale=cameraScale(camera),{width,height}=frame(camera);
  return camera.worldWidth*scale>width+.5||camera.worldHeight*scale>height+.5;
}
export function cameraScale(camera:Camera):number {return fitScale(camera)*camera.zoom;}
export function clampCamera(camera:Camera):Camera {
  camera.zoom=Math.min(MAX_ZOOM,Math.max(1,Number.isFinite(camera.zoom)?camera.zoom:1));
  const scale=cameraScale(camera),{width,height}=frame(camera);
  const clampAxis=(value:number,extent:number,visible:number)=>{
    const half=visible/scale/2;
    return extent<=half*2?extent/2:Math.min(extent-half,Math.max(half,Number.isFinite(value)?value:extent/2));
  };
  camera.x=clampAxis(camera.x,camera.worldWidth,width);
  camera.y=clampAxis(camera.y,camera.worldHeight,height);
  return camera;
}
export function resizeCamera(camera:Camera,viewWidth:number,viewHeight:number):Camera {
  camera.viewWidth=Math.max(1,viewWidth);camera.viewHeight=Math.max(1,viewHeight);
  return clampCamera(camera);
}
export function setInsets(camera:Camera,insets:Insets):Camera {
  camera.insets={...insets};return clampCamera(camera);
}
export function worldToScreen(camera:Camera,point:Vec):Vec {
  const scale=cameraScale(camera),{cx,cy}=frame(camera);
  return {x:(point.x-camera.x)*scale+cx,y:(point.y-camera.y)*scale+cy};
}
export function screenToWorld(camera:Camera,point:Vec):Vec {
  const scale=cameraScale(camera),{cx,cy}=frame(camera);
  return {x:(point.x-cx)/scale+camera.x,y:(point.y-cy)/scale+camera.y};
}
/** Multiply the zoom, keeping the world point under `screen` fixed when possible. */
export function zoomAt(camera:Camera,factor:number,screen:Vec):Camera {
  if(!(factor>0)||!Number.isFinite(factor))return camera;
  const anchor=screenToWorld(camera,screen);
  camera.zoom=Math.min(MAX_ZOOM,Math.max(1,camera.zoom*factor));
  const scale=cameraScale(camera),{cx,cy}=frame(camera);
  camera.x=anchor.x-(screen.x-cx)/scale;camera.y=anchor.y-(screen.y-cy)/scale;
  return clampCamera(camera);
}
export function panBy(camera:Camera,dx:number,dy:number):Camera {
  const scale=cameraScale(camera);
  camera.x-=dx/scale;camera.y-=dy/scale;
  return clampCamera(camera);
}
export function centerOn(camera:Camera,point:Vec):Camera {
  camera.x=point.x;camera.y=point.y;return clampCamera(camera);
}
export function resetCamera(camera:Camera):Camera {
  camera.zoom=1;camera.x=camera.worldWidth/2;camera.y=camera.worldHeight/2;return clampCamera(camera);
}
