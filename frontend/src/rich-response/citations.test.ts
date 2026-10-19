// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createCitationView, safeCitationUrl } from './citations';
import type { CitationBlock } from './citations';

const fixture = (): CitationBlock => ({ type: 'citations', id: 'sources', title: 'Supplied sources', sources: [
  { id: 'guide', title: 'Fixture title', url: 'https://fixture.example/guide', publisher: 'Fixture publisher', excerpt: 'Supplied excerpt; not verified.' },
] });
function host() { const el = document.createElement('div'); document.body.append(el); return el; }
beforeEach(() => {
  // jsdom has no native modal implementation; actual focus trapping is browser-owned.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false; } });
});
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('source-linked citations', () => {
  it.each(['javascript:alert(1)', 'data:text/html,evil', 'file:///tmp/private', '/relative', '//fixture.example', 'https://user:secret@fixture.example', 'https://fixture.example\\@evil.example', 'https://fixture.example/\nscript', ' https://fixture.example', 'https://', 'https://fixture.example/' + 'x'.repeat(2048)])('refuses %s', value => {
    expect(safeCitationUrl(value)).toBeNull();
    const root = host(), view = createCitationView(root), data = fixture(); data.sources[0].url = value;
    view.update([data]); view.renderBlock(root, 'sources'); root.querySelector('button')!.click();
    expect(root.querySelector('button')!.disabled).toBe(true); expect(document.querySelector('dialog,a')).toBeNull(); view.dispose();
  });
  it('admits only absolute HTTP(S) supplied URLs', () => {
    expect(safeCitationUrl('http://fixture.example/guide')).toBe('http://fixture.example/guide');
    expect(safeCitationUrl('HTTPS://fixture.example/guide')).toBe('https://fixture.example/guide');
  });
  it('keeps malicious source metadata inert and never requests previews', () => {
    const fetch = vi.spyOn(globalThis, 'fetch'); const root = host(), view = createCitationView(root), data = fixture();
    data.sources[0].title = '<img src=x onerror=alert(1)>'; data.sources[0].publisher = '<script>alert(1)</script>'; data.sources[0].excerpt = '<iframe src=evil>'; view.update([data]);
    view.renderInline(root, 'Claim [[cite:sources/guide]] <svg onload=evil>'); root.querySelector('button')!.click();
    const dialog = document.querySelector('dialog')!;
    expect(document.querySelector('img,script,iframe,svg')).toBeNull(); expect(dialog.textContent).toContain(data.sources[0].excerpt);
    const link = dialog.querySelector('a')!; expect(link.href).toBe(data.sources[0].url); expect(link.rel).toBe('noopener noreferrer'); expect(link.target).toBe('_blank');
    expect(fetch).not.toHaveBeenCalled(); expect(dialog.textContent).not.toMatch(/verified source|verification passed/i); view.dispose();
  });
  it('closes via keyboard cancel and close control, restores focus and tears down detached triggers', () => {
    const root = host(), view = createCitationView(root); view.update([fixture()]); const mount = view.renderInline(root, '[[cite:sources/guide]]');
    const trigger = root.querySelector('button')!; trigger.focus(); trigger.click();
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog'); expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(document.querySelector('dialog button'));
    document.querySelector('dialog')!.dispatchEvent(new Event('cancel', { cancelable: true }));
    expect(document.querySelector('dialog')).toBeNull(); expect(document.activeElement).toBe(trigger); expect(trigger.getAttribute('aria-expanded')).toBe('false');
    trigger.click(); document.querySelector<HTMLButtonElement>('dialog button')!.click(); expect(document.querySelector('dialog')).toBeNull();
    trigger.click(); mount.dispose(); mount.dispose(); expect(document.querySelector('dialog')).toBeNull(); trigger.click(); expect(document.querySelector('dialog')).toBeNull(); view.dispose(); view.dispose();
  });
  it('keeps unavailable refs visible, resolves later snapshots, preserves focus on equivalent replay', () => {
    const root = host(), view = createCitationView(root); view.renderInline(root, 'Claim [[cite:sources/guide]] [[cite:missing/nope]]');
    const trigger = root.querySelector('button')!; expect(trigger.disabled).toBe(true); expect(root.textContent).toContain('Source unavailable');
    view.update([fixture()]); expect(trigger.disabled).toBe(false); trigger.focus(); trigger.click();
    const modal = document.querySelector('dialog'); view.update(JSON.parse(JSON.stringify([fixture()])));
    expect(root.querySelector('button')).toBe(trigger); expect(document.querySelector('dialog')).toBe(modal);
    const changed = fixture(); changed.sources[0].excerpt = 'Changed evidence'; view.update([changed]); expect(document.querySelector('dialog')).toBeNull(); expect(document.activeElement).toBe(trigger);
    view.update([]); expect(trigger.disabled).toBe(true); view.dispose();
  });
  it('uses explicit complete tokens only and reconstructs from supplied data on reload', () => {
    const root = host(), view = createCitationView(root); view.update([fixture()]); let mount = view.renderInline(root, 'Partial [[cite:sources/gui');
    expect(root.querySelector('button')).toBeNull(); expect(root.textContent).toContain('[[cite:sources/gui'); mount.dispose();
    mount = view.renderInline(root, 'Claim [[cite:sources/guide]] [guide] [[cite:bad.id/guide]]');
    expect(root.querySelectorAll('button')).toHaveLength(1); expect(root.textContent).toContain('[[cite:bad.id/guide]]');
    const reload = host(), next = createCitationView(reload); next.update([fixture()]); next.renderInline(reload, 'Claim [[cite:sources/guide]] [guide] [[cite:bad.id/guide]]');
    expect(reload.textContent).toBe(root.textContent); expect(document.querySelector('dialog')).toBeNull(); view.dispose(); next.dispose();
  });
  it('bounds chip copy, snapshots read-only sources, and scopes disposal to the owning mount', () => {
    const root = host(), inline = host(), view = createCitationView(root), data = fixture();
    data.sources[0].title = 'Long supplied title '.repeat(40); delete data.sources[0].excerpt;
    const title = data.sources[0].title; view.update([data]);
    const blockMount = view.renderBlock(root, 'sources'); view.renderInline(inline, '[[cite:sources/guide]]');
    const trigger = inline.querySelector('button')!; expect(Array.from(trigger.textContent!).length).toBe(17);
    expect(trigger.getAttribute('aria-label')).toContain(title);
    data.sources[0].title = 'Mutated caller object'; trigger.click();
    const modal = document.querySelector('dialog')!; expect(modal.textContent).toContain(title); expect(modal.textContent).toContain('No excerpt supplied.');
    blockMount.dispose(); expect(document.querySelector('dialog')).toBe(modal);
    view.dispose(); expect(document.querySelector('dialog')).toBeNull();
  });
  it('fails closed on duplicate block/source identity, and preserves keyed block nodes on update', () => {
    const root = host(), view = createCitationView(root); view.update([fixture()]); view.renderBlock(root, 'sources'); const trigger = root.querySelector('button')!; trigger.focus();
    view.update([fixture()]); expect(root.querySelector('button')).toBe(trigger); expect(document.activeElement).toBe(trigger);
    const data = fixture(); data.sources.push({ ...data.sources[0] }); view.update([data]); expect(root.querySelector('button')).toBeNull();
    view.update([fixture(), fixture()]); expect(root.textContent).toContain('Source unavailable');
    view.dispose(); view.update([fixture()]); expect(root.querySelector('button')).toBeNull();
  });
});


it('retains late citation blocks and all supplied sources',()=>{
  const root=host(),view=createCitationView(root);
  const blocks=Array.from({length:25},(_,i)=>({...fixture(),id:'source'+i,sources:Array.from({length:33},(_,s)=>({...fixture().sources[0],id:'item'+s}))}));
  view.update(blocks);view.renderInline(root,'Late [[cite:source24/item32]]');
  expect(root.querySelector('button')!.disabled).toBe(false);
  view.dispose();
});
