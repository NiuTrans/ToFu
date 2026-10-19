// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { updateAnswerNavigation } from './answer-navigation';
afterEach(() => document.body.replaceChildren());

it('keeps focused navigation stable through replay and jumps to the current heading', () => {
  const toolbar = document.createElement('div'), body = document.createElement('div');
  document.body.append(toolbar, body);
  body.innerHTML = '<h1>Report</h1><h2>Evidence</h2><h2>Next steps</h2>';
  const ports = { renderSafeMarkdownHtml: (text: string) => text };
  updateAnswerNavigation(toolbar, body, ports);
  const outline = toolbar.querySelector('details')!;
  outline.open = true;
  const control = toolbar.querySelectorAll('button')[1]; control.focus();
  // Markdown may reconcile/replace a heading while the reader is in the menu.
  body.innerHTML = '<h1>Report</h1><h2>Updated evidence</h2><h2>Next steps</h2>';
  updateAnswerNavigation(toolbar, body, ports);
  expect(document.activeElement).toBe(control);
  expect(control.textContent).toBe('Updated evidence');
  const target = body.children[1] as HTMLElement;
  target.scrollIntoView = vi.fn();
  control.click();
  expect(outline.open).toBe(false);
  expect(document.activeElement).toBe(target);
  expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'start', behavior: 'auto' });
});

it('supports Escape, bounds long outlines and omits navigation for a short answer', () => {
  const toolbar = document.createElement('div'), body = document.createElement('div');
  document.body.append(toolbar, body);
  body.innerHTML = Array.from({ length: 80 }, (_, i) => `<h2>Section ${i}</h2>`).join('');
  const ports = { renderSafeMarkdownHtml: (text: string) => text };
  updateAnswerNavigation(toolbar, body, ports);
  expect(toolbar.querySelectorAll('button')).toHaveLength(32);
  const outline = toolbar.querySelector('details')!; outline.open = true;
  toolbar.querySelector('button')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  expect(outline.open).toBe(false);
  expect(document.activeElement).toBe(outline.querySelector('summary'));
  body.innerHTML = '<p>A short answer.</p>';
  updateAnswerNavigation(toolbar, body, ports);
  expect(toolbar.querySelector('details')).toBeNull();
});
