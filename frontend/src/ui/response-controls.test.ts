// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { createDialog, createChoice } from './index';
describe('response shared controls', () => {
  it('cycles Tab on dialog controls without leaving focus on the document body', () => {
    const link = document.createElement('a'); link.href = 'https://fixture.example'; link.textContent = 'Source';
    const handle = createDialog({ title: 'Source', body: link, actions: [{ id: 'close', label: 'Close' }] });
    document.body.append(handle.el); handle.el.setAttribute('open', '');
    const last = handle.el.querySelector('button')!; last.focus();
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }); last.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(true); expect(document.activeElement).toBe(link);
    link.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(last); handle.el.remove();
  });
  it('mounts radio controls into the host document through the kit', () => {
    const doc = document.implementation.createHTMLDocument();
    const choice = createChoice({ type: 'radio', label: '<script>inert</script>', value: 'Owned', name: 'scope' }, doc);
    expect(choice.el.ownerDocument).toBe(doc); expect(choice.input.ownerDocument).toBe(doc);
    expect(choice.el.className).toBe('ui-choice'); expect(choice.el.querySelector('script')).toBeNull();
    expect(choice.el.textContent).toBe('<script>inert</script>');
  });
});
