/** Two-finger PDF zoom. Native one-finger scrolling and text selection remain
 * available. One pending frame belongs to the reader's existing lifecycle.
 */
import type { LifecycleScope } from '../../lifecycle';

interface Point { x: number; y: number }
interface ZoomPorts<Anchor> {
  scale(): number;
  point(x: number, y: number): Anchor | null;
  zoom(anchor: Anchor, scale: number, x: number, y: number): boolean;
  active(value: boolean): void;
}

export function bindPdfPinchZoom<Anchor>(container: HTMLElement, scope: LifecycleScope, ports: ZoomPorts<Anchor>): void {
  let gesture: { anchor: Anchor; distance: number; scale: number } | null = null;
  let pending: { center: Point; distance: number } | null = null;
  let frame = 0;
  const geometry = (touches: TouchList): { center: Point; distance: number } => {
    const a = touches[0]!; const b = touches[1]!;
    return { center: { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 },
      distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) };
  };
  const apply = (): void => {
    frame = 0;
    const next = pending; pending = null;
    if (gesture && next && !ports.zoom(gesture.anchor, gesture.scale * next.distance / gesture.distance,
      next.center.x, next.center.y)) finish(false);
  };
  const finish = (flush: boolean): void => {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    if (flush) apply();
    pending = null; gesture = null; ports.active(false);
  };
  scope.listen(container, 'touchstart', (event) => {
    const touch = event as TouchEvent;
    if (touch.touches.length !== 2) { if (gesture) finish(false); return; }
    const start = geometry(touch.touches);
    const anchor = ports.point(start.center.x, start.center.y);
    if (!anchor || start.distance < 10) return;
    touch.preventDefault();
    gesture = { anchor, distance: start.distance, scale: ports.scale() };
    ports.active(true);
  }, { passive: false });
  scope.listen(container, 'touchmove', (event) => {
    const touch = event as TouchEvent;
    if (!gesture || touch.touches.length !== 2) return;
    touch.preventDefault(); pending = geometry(touch.touches);
    if (!frame) frame = requestAnimationFrame(apply);
  }, { passive: false });
  scope.listen(container, 'touchend', () => { if (gesture) finish(true); });
  scope.listen(container, 'touchcancel', () => finish(false));
  scope.listen(window, 'blur', () => { if (gesture) finish(false); });
  scope.listen(document, 'visibilitychange', () => { if (document.hidden && gesture) finish(false); });
  scope.add(() => finish(false));
}
