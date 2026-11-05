// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { createDiagnosticsMessageReconciler } from './diagnostics-message-reconciliation';

interface Message { id: string; content: string; metadata?: unknown }
function fixture() {
  const host = document.createElement('div');
  document.body.append(host);
  let creations = 0;
  const owner = createDiagnosticsMessageReconciler<Message>({
    identity: message => message.id,
    summarize: (message, index) => `${index + 1}: ${message.content}`,
    create(message, index) {
      creations++;
      const block = document.createElement('div');
      block.className = 'debug-msg-block';
      block.innerHTML = '<button class="debug-msg-header"><span class="debug-msg-summary"></span></button>'
        + '<div class="debug-msg-body"><pre></pre><details class="debug-raw"><summary>JSON</summary></details></div>';
      block.querySelector('.debug-msg-summary')!.textContent = `${index + 1}: ${message.content}`;
      block.querySelector('pre')!.textContent = message.content;
      return block;
    },
    open(block) {
      expect(block._msgRef.content).toBeDefined();
      block.classList.add('open');
    },
  });
  return { host, owner, creations: () => creations };
}
const message = (id: string, content = id): Message => ({ id, content });
const header = (block: Element) => block.querySelector<HTMLButtonElement>('.debug-msg-header')!;
const raw = (block: Element) => block.querySelector<HTMLDetailsElement>('.debug-raw')!;
afterEach(() => { document.body.replaceChildren(); document.documentElement.lang = ''; });

describe('diagnostic message reconciliation', () => {
  it('retains unchanged nodes, latest references, focus and selection while replacing dividers', () => {
    const { host, owner, creations } = fixture();
    const messages = [message('a'), message('b')];
    owner.render(host, messages, { leading: [document.createElement('hr')] });
    const first = host.children[1];
    first.classList.add('open'); raw(first).open = true; header(first).focus();
    const text = first.querySelector('pre')!.firstChild!;
    window.getSelection()!.setBaseAndExtent(text, 0, text, 1);
    const next = messages.map(item => ({ ...item }));
    owner.render(host, next, { leading: [document.createElement('hr')] });
    expect(host.children[1]).toBe(first);
    expect(creations()).toBe(2);
    expect((first as HTMLElement & { _msgRef: Message })._msgRef).toBe(next[0]);
    expect(first.classList.contains('open') && raw(first).open).toBe(true);
    expect(document.activeElement).toBe(header(first));
    expect(window.getSelection()!.toString()).toBe('a');
  });

  it('updates equal-size text, metadata and in-place mutations without collapsing the reader', () => {
    const { host, owner, creations } = fixture();
    const value = message('a', 'old');
    owner.render(host, [value]);
    host.firstElementChild!.classList.add('open'); raw(host.firstElementChild!).open = true;
    header(host.firstElementChild!).focus();
    value.content = 'new'; owner.render(host, [value]);
    expect(host.querySelector('pre')!.textContent).toBe('new');
    expect(host.firstElementChild!.classList.contains('open')).toBe(true);
    expect(raw(host.firstElementChild!).open).toBe(true);
    expect(document.activeElement).toBe(header(host.firstElementChild!));
    value.metadata = { toolArguments: 'new' }; owner.render(host, [value]);
    expect(creations()).toBe(3);
  });

  it('matches duplicate identities by exact content before assigning changed occurrences', () => {
    const { host, owner } = fixture();
    owner.render(host, [message('same', 'alpha'), message('same', 'bravo')]);
    const alpha = host.children[0], bravo = host.children[1];
    alpha.classList.add('open'); header(alpha).focus();
    owner.render(host, [message('same', 'changed'), message('same', 'alpha')]);
    expect(host.children[1]).toBe(alpha);
    expect(host.children[0]).not.toBe(bravo);
    expect(host.children[0].classList.contains('open')).toBe(false);
    expect(host.children[1].classList.contains('open')).toBe(true);
    expect(document.activeElement).toBe(header(alpha));
    expect(alpha.querySelector('.debug-msg-summary')!.textContent).toBe('2: alpha');
  });

  it('uses each identical occurrence once and preserves focus on the second occurrence', () => {
    const { host, owner, creations } = fixture();
    owner.render(host, [message('same'), message('same')]);
    const [first, second] = Array.from(host.children);
    second.classList.add('open'); header(second).focus();
    owner.render(host, [message('same'), message('same'), message('same')]);
    expect(Array.from(host.children).slice(0, 2)).toEqual([first, second]);
    expect(document.activeElement).toBe(header(second));
    expect(first.classList.contains('open')).toBe(false);
    expect(creations()).toBe(3);
  });

  it('reorders and removes messages, updates ordinals, and keeps selected unchanged evidence', () => {
    const { host, owner } = fixture();
    owner.render(host, [message('a', 'alpha'), message('b', 'bravo'), message('c')]);
    const [alpha, bravo, removed] = Array.from(host.children);
    const text = bravo.querySelector('pre')!.firstChild!;
    window.getSelection()!.setBaseAndExtent(text, 5, text, 0);
    owner.render(host, [message('b', 'bravo'), message('a', 'alpha')]);
    expect(Array.from(host.children)).toEqual([bravo, alpha]);
    expect(removed.isConnected).toBe(false);
    expect(bravo.getAttribute('data-idx')).toBe('0');
    expect(window.getSelection()!.anchorOffset).toBe(5);
    expect(window.getSelection()!.toString()).toBe('bravo');
  });

  it('interleaves the complete ordered frame and retires obsolete notes and messages', () => {
    const { host, owner } = fixture();
    const warning = document.createElement('aside'), before = document.createElement('hr'), after = document.createElement('hr');
    owner.render(host, [message('a')], { leading: [warning],
      beforeMessage: new Map([[0, [before]], [1, [after]]]) });
    expect(Array.from(host.children).map(n => n.tagName)).toEqual(['ASIDE', 'HR', 'DIV', 'HR']);
    owner.render(host, []);
    expect(host.childNodes.length).toBe(0);
    expect([warning, before, after].every(n => !n.isConnected)).toBe(true);
  });

  it('resets on demand, never revives detached nodes or shares nodes between hosts', () => {
    const { host, owner } = fixture();
    owner.render(host, [message('a')]); const first = host.firstElementChild;
    owner.render(host, [message('a')], { reuse: false });
    expect(host.firstElementChild).not.toBe(first);
    const secondHost = document.createElement('div');
    owner.render(secondHost, [message('a')]);
    expect(secondHost.firstElementChild).not.toBe(host.firstElementChild);
    const retired = host.firstElementChild;
    host.replaceChildren(); owner.render(host, [message('a')]);
    expect(host.firstElementChild).not.toBe(retired);
  });

  it('preserves the existing positional streaming policy for identifier-less edits', () => {
    const { host, owner } = fixture();
    owner.render(host, [message('old-key', 'old')]);
    host.firstElementChild!.classList.add('open');
    owner.render(host, [message('new-key', 'new')], { reuse: 'position' });
    expect(host.firstElementChild!.classList.contains('open')).toBe(true);
    expect(host.firstElementChild!.getAttribute('data-mid')).toBe('new-key');
  });

  it('rebuilds translated presentation after a language change, preserving disclosure state', () => {
    const { host, owner, creations } = fixture();
    document.documentElement.lang = 'en'; owner.render(host, [message('a')]);
    host.firstElementChild!.classList.add('open');
    document.documentElement.lang = 'zh-CN'; owner.render(host, [message('a')]);
    expect(creations()).toBe(2);
    expect(host.firstElementChild!.classList.contains('open')).toBe(true);
  });

  it('never treats an unserializable non-wire object as unchanged', () => {
    const { host, owner, creations } = fixture();
    const cyclic = message('a'); cyclic.metadata = cyclic;
    owner.render(host, [cyclic]); owner.render(host, [cyclic]);
    expect(creations()).toBe(2);
  });
});
