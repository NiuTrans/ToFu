/** Individually controllable office props (chairs, books) for the scene canvas.
 * Every prop is a separate object with its own id, kind, position and eased
 * slide motion, so the sequencer can move any furniture part on its own.
 * Pure state and math here — no DOM, no timers; the scene's rAF tick steps
 * motion and draws. Atlas: assets/props.png, derived by
 * scripts/generate_evolution_props.py (PROPS below is its printed output).
 */
import type { Point } from './office-navigation';
import { CHAIR_FRAME } from './office-game-frames';
export type PropKind = 'chair' | 'bookFlat0' | 'bookFlat1' | 'bookFlat2' | 'bookFlat3' | 'bookUp0' | 'bookUp1' | 'bookUp2' | 'bookUp3';
export const KINDS: PropKind[] = ['chair', 'bookFlat0', 'bookFlat1', 'bookFlat2', 'bookFlat3', 'bookUp0', 'bookUp1', 'bookUp2', 'bookUp3'];
// Measured alpha bounds of the nine-prop atlas. Regenerate with
// scripts/generate_evolution_props.py; no hand-edited raster copies.
export const PROPS = [
  [28, 14, 57, 86],
  [35, 142, 42, 18],
  [35, 238, 42, 18],
  [35, 334, 42, 18],
  [35, 430, 42, 18],
  [42, 520, 27, 32],
  [42, 616, 27, 32],
  [42, 712, 27, 32],
  [42, 808, 27, 32],
];
/** Drawn heights as a fraction of the actor sprite height, so props scale with the viewport. */
export const PROP_SCALE: Record<'chair' | 'bookFlat' | 'bookUp', number> = { chair: .47, bookFlat: .11, bookUp: .19 };
export type Prop = Point & {
  id: string; kind: PropKind; from: Point; to: Point; elapsed: number; duration: number; hidden: boolean;
};
export function createProp(id: string, kind: PropKind, at: Point): Prop {
  return { id, kind, x: at.x, y: at.y, from: { ...at }, to: { ...at }, elapsed: 0, duration: 0, hidden: false };
}
/** Slide a prop to a point over duration ms (ease in-out). duration 0 places it. */
export function slideProp(prop: Prop, to: Point, duration: number) {
  prop.from = { x: prop.x, y: prop.y };
  prop.to = { ...to };
  prop.elapsed = 0;
  prop.duration = Math.max(0, duration);
  if (!prop.duration) { prop.x = to.x; prop.y = to.y; }
}
export function placeProp(prop: Prop, to: Point) { slideProp(prop, to, 0); }
export function propSettled(prop: Prop): boolean { return prop.elapsed >= prop.duration; }
/** Advance every prop; returns true while any prop is still sliding. */
export function stepProps(props: Iterable<Prop>, dtMs: number): boolean {
  let moving = false;
  for (const prop of props) {
    if (propSettled(prop)) continue;
    prop.elapsed = Math.min(prop.duration, prop.elapsed + dtMs);
    const t = prop.duration ? prop.elapsed / prop.duration : 1;
    const eased = t < .5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2;
    prop.x = prop.from.x + (prop.to.x - prop.from.x) * eased;
    prop.y = prop.from.y + (prop.to.y - prop.from.y) * eased;
    moving = moving || !propSettled(prop);
  }
  return moving;
}
/** Draw a prop anchored bottom-center at its position, height in scene px. */
export function drawProp(ctx: CanvasRenderingContext2D, atlas: HTMLImageElement, prop: Prop, height: number) {
  if (prop.hidden || !atlas.complete || !atlas.naturalWidth) return;
  const [sx, sy, sw, sh] = PROPS[KINDS.indexOf(prop.kind)];
  const width = sw / sh * height;
  ctx.drawImage(atlas, sx, sy, sw, sh, Math.round(prop.x - width / 2), Math.round(prop.y - height), width, height);
}
/** Authored upholstered rolling chair, anchored at its wheels. */
export function drawOfficeChair(ctx: CanvasRenderingContext2D, art: HTMLImageElement, x: number, y: number, height: number) {
  if (!art.complete || !art.naturalWidth) return;
  const [sx, sy, sw, sh] = CHAIR_FRAME;
  const width = sw / sh * height;
  ctx.fillStyle = 'rgba(42,32,21,.16)';
  ctx.beginPath();ctx.ellipse(x,y-1,width*.38,Math.max(2,height*.075),0,0,Math.PI*2);ctx.fill();
  ctx.drawImage(art,sx,sy,sw,sh,Math.round(x-width/2),Math.round(y-height),width,height);
}
