/**
 * Backdrop-dismissal regression: a click that ends outside the dialog only
 * dismisses it when the gesture STARTED outside too. A text-selection drag
 * begins inside, crosses the boundary, and releases outside — that click must
 * leave the dialog open, otherwise selecting content near the edge loses it.
 */

// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';

import { createDialog } from './dialog';
import { createDrawer } from './drawer';

const BOX = new DOMRect(100, 100, 300, 200); // left, top, width, height
const INSIDE = { clientX: 150, clientY: 150 };
const OUTSIDE = { clientX: 10, clientY: 10 };

function setup(el: HTMLElement) {
  el.getBoundingClientRect = () => BOX;
  // jsdom does not implement HTMLDialogElement.close(); provide it so the
  // dismissal path can call it and the spy can observe the call.
  (el as HTMLDialogElement).close = () => {};
  const closeSpy = vi.spyOn(el as HTMLDialogElement, 'close');
  return closeSpy;
}

function fire(el: HTMLElement, type: string, coords: { clientX: number; clientY: number }): void {
  el.dispatchEvent(new MouseEvent(type, { bubbles: true, ...coords }));
}

describe('backdrop dismissal requires the gesture to start outside', () => {
  it('dialog: dismisses on a genuine backdrop click (outside → outside)', () => {
    const dialog = createDialog({ title: 't', body: 'b' });
    const closeSpy = setup(dialog.el);
    fire(dialog.el, 'mousedown', OUTSIDE);
    fire(dialog.el, 'click', OUTSIDE);
    expect(closeSpy).toHaveBeenCalledTimes(1);
  });

  it('dialog: stays open when a selection drag starts inside and releases outside', () => {
    const dialog = createDialog({ title: 't', body: 'b' });
    const closeSpy = setup(dialog.el);
    fire(dialog.el, 'mousedown', INSIDE);
    fire(dialog.el, 'click', OUTSIDE);
    expect(closeSpy).not.toHaveBeenCalled();
  });

  it('dialog: stays open for a click fully inside the box', () => {
    const dialog = createDialog({ title: 't', body: 'b' });
    const closeSpy = setup(dialog.el);
    fire(dialog.el, 'mousedown', INSIDE);
    fire(dialog.el, 'click', INSIDE);
    expect(closeSpy).not.toHaveBeenCalled();
  });

  it('drawer: dismisses on a genuine backdrop click (outside → outside)', () => {
    const drawer = createDrawer({ title: 't', body: 'b' });
    const closeSpy = setup(drawer.el);
    fire(drawer.el, 'mousedown', OUTSIDE);
    fire(drawer.el, 'click', OUTSIDE);
    expect(closeSpy).toHaveBeenCalled();
  });

  it('drawer: stays open when a selection drag starts inside and releases outside', () => {
    const drawer = createDrawer({ title: 't', body: 'b' });
    const closeSpy = setup(drawer.el);
    fire(drawer.el, 'mousedown', INSIDE);
    fire(drawer.el, 'click', OUTSIDE);
    expect(closeSpy).not.toHaveBeenCalled();
  });
});
