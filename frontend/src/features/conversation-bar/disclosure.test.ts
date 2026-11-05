// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { createAnchoredDisclosure, positionDisclosure } from './disclosure';

it('opens on the first enabled control that is rendered, skipping hidden ones', () => {
  const anchor = document.createElement('button'); document.body.append(anchor);
  const panel = document.createElement('div');
  const disabled = document.createElement('button'); disabled.disabled = true;
  const hidden = document.createElement('button'); hidden.hidden = true;
  const nested = document.createElement('span'); nested.hidden = true; nested.append(document.createElement('button'));
  const visible = document.createElement('button');
  panel.append(disabled, hidden, nested, visible);
  const disclosure = createAnchoredDisclosure({ anchor, panel });
  try {
    disclosure.open();
    expect(document.activeElement).toBe(visible);
  } finally { disclosure.destroy(); anchor.remove(); }
});

it('keeps an optionally constrained disclosure inside its visible horizontal surface', () => {
  const anchor = document.createElement('button'), panel = document.createElement('div');
  const surface = document.createElement('div'); surface.append(anchor, panel); document.body.append(surface);
  anchor.getBoundingClientRect = () => ({left: 620, right: 680, top: 10, bottom: 40, width: 60, height: 30}) as DOMRect;
  panel.getBoundingClientRect = () => ({width: 440, height: 100}) as DOMRect;
  surface.getBoundingClientRect = () => ({left: 590, right: 1000, width: 410}) as DOMRect;
  try {
    positionDisclosure(anchor, panel, surface);
    const left = parseFloat(panel.style.left), limit = parseFloat(panel.style.maxWidth);
    expect(left).toBeGreaterThanOrEqual(590);
    expect(left + Math.min(440, limit)).toBeLessThanOrEqual(1000);
    positionDisclosure(anchor, panel);
    expect(parseFloat(panel.style.left) + 440).toBe(680);
  } finally { surface.remove(); }
});
