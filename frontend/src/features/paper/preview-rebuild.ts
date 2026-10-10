/** Debounced rebuild nudge for the chat paper preview.
 *
 * The preview panel registers the SAME callback its compile button uses
 * (options.refresh, the controller's requeue path) while it is mounted.
 * Track-B surfaces that change document bytes outside the chat receipt
 * stream — the source editor's save and the proposal card's accept — call
 * nudgePreviewRebuild() so the PDF recompiles through the existing build
 * submission flow instead of a parallel one. Calls collapse into one
 * trailing rebuild per 500ms window. */

let registered: (() => void) | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;

export const PREVIEW_REBUILD_DEBOUNCE_MS = 500;

export function registerPreviewRebuild(rebuild: () => void): () => void {
  registered = rebuild;
  return () => {
    if (registered === rebuild) registered = null;
    if (timer && !registered) {
      clearTimeout(timer);
      timer = null;
    }
  };
}

/** Schedule a rebuild when a panel is mounted; a no-op otherwise. */
export function nudgePreviewRebuild(): void {
  if (!registered || timer) return;
  timer = setTimeout(() => {
    timer = null;
    registered?.();
  }, PREVIEW_REBUILD_DEBOUNCE_MS);
}
