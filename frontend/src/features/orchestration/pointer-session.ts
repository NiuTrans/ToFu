import { orchestrationRegistry } from './registry';
interface PointerSessionTarget {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
}

export interface OrchestrationPointerSessionOptions {
  pointerId?: number | null;
  moveTarget?: PointerSessionTarget | null;
  pointerTarget?: PointerSessionTarget | null;
  captureTarget?: PointerSessionTarget | null;
  window?: PointerSessionTarget | null;
  onMove?: (event: Event) => unknown;
  onEnd?: (event: Event) => unknown;
}

type PointerSessionWindow = Window & {
  bindOrchestrationPointerSession?: typeof bindOrchestrationPointerSession;
};

type PointerEventLike = Event & { pointerId?: number | null };
type Binding = [PointerSessionTarget, string, EventListener];

/** Bind one pointer gesture and return its idempotent lifecycle disposer. */
export function bindOrchestrationPointerSession(
  options: OrchestrationPointerSessionOptions = {},
): () => boolean {
  const bindings: Binding[] = [];
  let stopped = false;
  const pointerId = options.pointerId;

  const add = (
    target: PointerSessionTarget | null | undefined,
    type: string,
    listener: EventListener,
  ): void => {
    if (!target || typeof target.addEventListener !== 'function') return;
    target.addEventListener(type, listener);
    bindings.push([target, type, listener]);
  };
  const matches = (event?: PointerEventLike): boolean => !event
    || event.type === 'blur'
    || pointerId == null
    || event.pointerId == null
    || event.pointerId === pointerId;
  const move: EventListener = (value) => {
    const event = value as PointerEventLike;
    if (!stopped && matches(event)) options.onMove?.(event);
  };
  const end: EventListener = (value) => {
    const event = value as PointerEventLike;
    if (!stopped && matches(event)) options.onEnd?.(event);
  };

  add(options.moveTarget, 'pointermove', move);
  add(options.pointerTarget, 'pointerup', end);
  add(options.pointerTarget, 'pointercancel', end);
  add(options.window, 'blur', end);
  add(options.captureTarget, 'lostpointercapture', end);

  return function unbind(): boolean {
    if (stopped) return false;
    stopped = true;
    bindings.splice(0).forEach(([target, type, listener]) => {
      target.removeEventListener(type, listener);
    });
    return true;
  };
}

(orchestrationRegistry as unknown as PointerSessionWindow).bindOrchestrationPointerSession =
  bindOrchestrationPointerSession;
