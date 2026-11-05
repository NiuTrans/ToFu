/**
 * Shared native-dialog backdrop gesture, used by modal and drawer factories.
 * Entry: bindDialogBackdrop. Listeners live with the dialog element; each
 * factory retains its own settlement semantics in the optional callback.
 */
export function bindDialogBackdrop(
  el: HTMLDialogElement,
  settle?: (result: null) => void,
): void {
  // Coordinate checks work when the browser retargets backdrop events. A
  // selection drag that starts inside and ends outside must keep the modal.
  let pressedOutside = false;
  const isOutsideDialog = (event: MouseEvent): boolean => {
    const rect = el.getBoundingClientRect();
    return (
      event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom
    );
  };
  el.addEventListener('mousedown', event => {
    pressedOutside = isOutsideDialog(event);
  });
  el.addEventListener('click', event => {
    if (pressedOutside && isOutsideDialog(event)) {
      settle?.(null);
      el.close('');
    }
    pressedOutside = false;
  });
}
