/** Main office layout over assets/office-clear.webp: 960×720 world units for
 * 1448×1086 art (1.508 art px per unit). Furniture hotspots with approach
 * points, resident stations, choreography points and prop anchors, all
 * verified against the art and the navigation grid. Pure data.
 * Navigation (office-navigation.ts) owns walkability.
 */
import type { Point } from './office-navigation';
import type { ActorRole } from './studio/projection';

export const WORLD = {width:960,height:720} as const;
export type Place = 'library'|'plan'|'work'|'review'|'delivery';
export const PLACES:Place[] = ['library','plan','work','review','delivery'];
/** rect = [x, y, width, height]; approach = walkable spot facing the furniture. */
export const HOTSPOTS:Record<Place,{rect:readonly [number,number,number,number];approach:Point;face:number;anchor:Point}> = {
  library:{rect:[400,26,162,84],approach:{x:481,y:136},face:3,anchor:{x:481,y:30}},
  plan:{rect:[12,128,66,120],approach:{x:160,y:272},face:1,anchor:{x:45,y:132}},
  work:{rect:[100,114,212,118],approach:{x:290,y:262},face:3,anchor:{x:206,y:116}},
  review:{rect:[644,114,202,118],approach:{x:660,y:262},face:3,anchor:{x:745,y:116}},
  delivery:{rect:[640,496,174,128],approach:{x:720,y:480},face:0,anchor:{x:727,y:498}},
};
export type Station = Point & {face:number;seat:boolean};
export const STATIONS:Record<ActorRole,Station> = {
  executor:{x:205,y:282,face:3,seat:true},
  reviewer:{x:750,y:282,face:3,seat:true},
  coordinator:{x:160,y:272,face:1,seat:false},
};
/** Idle residents wait beside their own furniture. */
export const HOMES:Record<ActorRole,Point> = {
  executor:{x:340,y:300},reviewer:{x:620,y:300},coordinator:{x:480,y:250},
};
/** When the human is needed the residents gather inside the front door. */
export const WAIT_POINTS:Record<ActorRole,Point> = {
  executor:{x:440,y:590},reviewer:{x:520,y:590},coordinator:{x:480,y:560},
};
export const SHELF_POINT:Point = HOTSPOTS.library.approach;
export const DESK_BOOK:Point = {x:282,y:176};
export const DESK_FRONT:Point = {x:290,y:262};
export const HANDOFF_POINT:Point = {x:290,y:300};
export const CONSULT_POINT:Point = {x:230,y:330};
export const DELEGATE_POINT:Point = {x:480,y:540};
/** Front entrance: the inside threshold is walkable, the doormat is not. */
export const DOOR_POINT:Point = {x:480,y:612};
export const OUTSIDE_POINT:Point = {x:480,y:704};
export const PLAYER_START:Point = {x:480,y:560};
/** Collected materials pile on the floor beside the bookshelf, two columns. */
export const SHELF_STACK = (slot:number):Point => ({x:578+(slot%2)*19,y:120-Math.floor(slot/2)*6});
export const MAX_BOOKS = 8;
/** Sub-agents stand at open-floor hot desks while their task is live. */
export const VISITOR_DESKS:Point[] = [
  {x:480,y:470},{x:560,y:470},{x:400,y:470},{x:480,y:530},{x:560,y:530},{x:650,y:420},{x:360,y:380},{x:600,y:360},
];
export const MAX_VISITORS = VISITOR_DESKS.length;
/** Screens painted in the art, lit from live stage state (world rects). */
export const MONITORS:Record<'executor'|'reviewer',readonly [number,number,number,number]> = {
  executor:[211,121,42,36],reviewer:[716,144,41,31],
};
export const DELIVERY_DOCUMENT:Point = {x:727,y:545};
/** Resident character height in world units (art-derived: ~124 art px). */
export const SPRITE_HEIGHT = 82;
export const INTERACT_RANGE = 70;
