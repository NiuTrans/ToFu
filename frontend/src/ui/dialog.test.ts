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

describe.each([
  ['dialog', () => createDialog({ title: 't', body: 'b' })],
  ['drawer', () => createDrawer({ title: 't', body: 'b' })],
] as const)('%s backdrop gesture boundary', (_name, create) => {
  it('ignores an outside click without an outside press', () => {
    const { el } = create();
    const close = setup(el);
    fire(el, 'click', OUTSIDE);
    expect(close).not.toHaveBeenCalled();
  });

  it('consumes a gesture even when it ends inside', () => {
    const { el } = create();
    const close = setup(el);
    fire(el, 'mousedown', OUTSIDE);
    fire(el, 'click', INSIDE);
    fire(el, 'click', OUTSIDE);
    expect(close).not.toHaveBeenCalled();
  });

  it('keeps the exact dialog edges inside its box', () => {
    const { el } = create();
    const close = setup(el);
    fire(el, 'mousedown', { clientX: BOX.left, clientY: BOX.top });
    fire(el, 'click', OUTSIDE);
    fire(el, 'mousedown', OUTSIDE);
    fire(el, 'click', { clientX: BOX.right, clientY: BOX.bottom });
    expect(close).not.toHaveBeenCalled();
  });

  it('keeps pressed state local to each control', () => {
    const first = create(), second = create();
    const closeFirst = setup(first.el), closeSecond = setup(second.el);
    fire(first.el, 'mousedown', OUTSIDE);
    fire(second.el, 'click', OUTSIDE);
    fire(first.el, 'click', OUTSIDE);
    expect(closeSecond).not.toHaveBeenCalled();
    expect(closeFirst).toHaveBeenCalledTimes(1);
    fire(first.el, 'click', OUTSIDE);
    expect(closeFirst).toHaveBeenCalledTimes(1);
  });

  it('measures on press and only measures a click after an outside press', () => {
    const { el } = create();
    const close = setup(el);
    const measure = vi.spyOn(el, 'getBoundingClientRect');
    fire(el, 'mousedown', INSIDE);
    fire(el, 'click', OUTSIDE);
    expect(measure).toHaveBeenCalledTimes(1);
    fire(el, 'mousedown', OUTSIDE);
    // Use the current geometry at release, rather than retaining press geometry.
    measure.mockReturnValue(new DOMRect(0, 0, 30, 30));
    fire(el, 'click', OUTSIDE);
    expect(measure).toHaveBeenCalledTimes(3);
    expect(close).not.toHaveBeenCalled();
  });
});

/** Platform semantics jsdom lacks: showModal refuses a detached dialog; close fires `close`. */
function nativeModal(el: HTMLDialogElement): void {
  el.showModal = () => {
    if (!el.isConnected) throw new DOMException('The element is not in a Document.', 'InvalidStateError');
    el.setAttribute('open', '');
  };
  el.close = (result?: string) => {
    if (!el.open) return;
    el.removeAttribute('open');
    el.returnValue = result ?? '';
    el.dispatchEvent(new Event('close'));
  };
}

describe('dialog mounting', () => {
  it('opens a dialog its caller never attached and removes it once settled', async () => {
    const dialog = createDialog({ title: 't', body: 'b', actions: [{ id: 'ok', label: 'OK' }] });
    nativeModal(dialog.el);
    const choice = dialog.open();
    expect(dialog.el.isConnected).toBe(true);
    expect(dialog.el.open).toBe(true);
    dialog.el.querySelector('button')!.click();
    await expect(choice).resolves.toBe('ok');
    expect(dialog.el.isConnected).toBe(false);
  });

  it('leaves a caller-mounted dialog where the caller put it', async () => {
    const host = document.createElement('div');
    document.body.append(host);
    const dialog = createDialog({ title: 't', body: 'b' });
    nativeModal(dialog.el);
    host.append(dialog.el);
    const choice = dialog.open();
    dialog.close('done');
    await expect(choice).resolves.toBe('done');
    expect(dialog.el.parentElement).toBe(host);
    host.remove();
  });
});
