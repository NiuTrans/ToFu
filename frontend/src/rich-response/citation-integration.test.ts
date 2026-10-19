// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { renderRichMarkdown, disposeRichMarkdown } from './markdown';
import { validateRichResponse } from './validation';
import { renderRichResponse } from './renderer';
const sources = () => ({ version: 1, title: 'Supplied evidence', blocks: [{ type: 'citations', id: 'sources', title: 'Sources', sources: [
  { id: 'guide', title: 'Guide', url: 'https://fixture.example/guide', publisher: 'Fixture', excerpt: 'Supplied, not fetched.' },
] }] });
const ports = { renderSafeMarkdownHtml: (source: string) => DOMPurify.sanitize(marked.parse(source, { async: false }) as string) };
function host() { const node = document.createElement('div'); document.body.append(node); return node; }
afterEach(() => document.body.replaceChildren());
describe('citations in the production response path', () => {
  it('activates complete prose references but not code or link labels, retains focus on replay', () => {
    const root = host();
    const prose = 'A claim [[cite:sources/guide]].\n\n`[[cite:sources/guide]]`\n\n<a href="https://fixture.example/link">[[cite:sources/guide]]</a>\n\n```text\n[[cite:sources/guide]]\n```\n\n';
    const source = prose + '```tofu-ui\n' + JSON.stringify(sources()) + '\n```';
    expect(renderRichMarkdown(root, source, true, ports, 'owned')).toBe(true);
    const button = root.querySelector<HTMLButtonElement>('.rich-citation-inline button')!;
    expect(root.querySelectorAll('.rich-citation-inline button')).toHaveLength(1);
    expect(root.querySelector('code')?.textContent).toContain('[[cite:');
    expect(button.disabled).toBe(false); button.focus();
    renderRichMarkdown(root, source, true, ports, 'owned');
    expect(root.querySelector('.rich-citation-inline button')).toBe(button);
    expect(document.activeElement).toBe(button);
    disposeRichMarkdown(root);
  });
  it('keeps references unavailable while evidence arrives and reconstructs after reload', () => {
    const root = host(); const partial = 'Claim [[cite:sources/guide]]\n\n```tofu-ui\n';
    renderRichMarkdown(root, partial, false, ports, 'owned');
    expect(root.querySelector<HTMLButtonElement>('.rich-citation-inline button')?.disabled).toBe(true);
    const complete = partial + JSON.stringify(sources()) + '\n```';
    renderRichMarkdown(root, complete, true, ports, 'owned');
    expect(root.querySelector<HTMLButtonElement>('.rich-citation-inline button')?.disabled).toBe(false);
    const next = host(); renderRichMarkdown(next, complete, true, ports, 'owned');
    expect(next.textContent).toBe(root.textContent);
    disposeRichMarkdown(root); disposeRichMarkdown(next);
  });
  it.each(['javascript:alert(1)', 'https://a:b@fixture.example', 'https://fixture.example\\path', 'https://', 'https://fixture.example:invalid'])('rejects unsafe supplied URL %s', url => {
    const response = sources(); response.blocks[0].sources[0].url = url;
    expect(validateRichResponse(response)).toBeNull();
    const root = host(); expect(renderRichResponse(root, response)).toBe(false);
    expect(root.querySelector('a,button,dialog')).toBeNull();
  });
  it('rejects duplicate source identities and keeps unknown refs visible', () => {
    const response = sources(); response.blocks[0].sources.push(response.blocks[0].sources[0]);
    expect(validateRichResponse(response)).toBeNull();
    const root = host(); renderRichMarkdown(root, 'Unknown [[cite:sources/missing]]\n\n```tofu-ui\n' + JSON.stringify(sources()) + '\n```', true, ports);
    expect(root.querySelector<HTMLButtonElement>('.rich-citation-inline button')?.disabled).toBe(true);
    expect(root.textContent).toContain('Source unavailable'); disposeRichMarkdown(root);
  });
});
