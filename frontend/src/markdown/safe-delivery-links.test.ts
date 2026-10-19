// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import DOMPurify from 'dompurify';
import { safeArtifactDownloadHref } from './safe-delivery-links';
import { projectMarkdownHtml } from './html-policy';
const path = '/api/artifacts/12345678-1234-1234-1234-123456789abc/download';
const origin = 'https://tofu.example';

describe('model links cannot opt into executable downloads', () => {
  it.each([
    path + '?interactive=1', path + '?interactive=0&interactive=1',
    '/proxy/15000' + path + '?interactive=1',
    origin + path + '?interactive=1', '//tofu.example' + path + '?interactive=1',
    path.replace('/api/', '/%61pi/') + '?%69nteractive=%31',
    path + '%3Finteractive%3D1', path + '?%2569nteractive=1',
    origin.replace('://', ':\\\\') + path + '?interactive=1',
    path.replace('/download', '/%2564ownload') + '?interactive%253D1',
    path + '?next=foo%26interactive%3D1',
  ])('forces safe mode for %s', href => {
    const result = safeArtifactDownloadHref(href, origin);
    expect(result).not.toMatch(/interactive|%/i);
    expect(new URL(result, origin).pathname).toMatch(/\/api\/artifacts\/[^/]+\/download$/);
  });
  it.each([path, path + '?download=1', path + '#file', 'https://external.example' + path + '?interactive=1',
    '/api/other/download?interactive=1', '/settings?interactive=1', 'articles/intro.html', '#part', 'mailto:me@example.org'])
    ('preserves normal links %s', href => expect(safeArtifactDownloadHref(href, origin)).toBe(href));
  it.each([
    '/api/response-assets/artifacts/id?conversationId=owned&download=1&interactive=1',
    '/proxy/15000/api/response-assets/artifacts/id?conversationId=owned&download=1&%69nteractive=1',
    origin + '/api/response-assets/artifacts/id?conversationId=owned&download=1&%2569nteractive=1',
    '//tofu.example/api/%72esponse-assets/artifacts/id?conversationId=owned&download=1&interactive=1',
  ])('retains ownership context on guarded resolver %s', href => {
    const url = new URL(safeArtifactDownloadHref(href, origin), origin);
    expect(url.searchParams.get('conversationId')).toBe('owned');
    expect(url.searchParams.get('download')).toBe('1');
    expect(url.searchParams.has('interactive')).toBe(false);
  });
  it('keeps the safe owned resolver unchanged', () => {
    const safe = '/api/response-assets/artifacts/id?conversationId=owned&download=1';
    expect(safeArtifactDownloadHref(safe, origin)).toBe(safe);
  });
  it('guards HTML-entity query selectors in the sanitized pipeline', () => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml('<a href="/api/response-assets/artifacts/id?conversationId=owned&amp;download=1&amp;interactive=1">Original</a>', {
      document, parser: null, sanitizer: DOMPurify, highlighter: null, mathRenderer: null,
      basePath: '/proxy/15000', projectActive: false,
    });
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/proxy/15000/api/response-assets/artifacts/id?conversationId=owned&download=1');
  });
  it.each([
    `<map><area href="${path}?interactive=1" alt="Original"></map>`,
    `<svg><a xlink:href="${path}?interactive=1"><text>Original</text></a></svg>`,
  ])('guards other sanitizer-admitted clickable URI attributes', html => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml(html, { document, parser: null, sanitizer: DOMPurify,
      highlighter: null, mathRenderer: null, basePath: '', projectActive: false });
    expect(root.innerHTML).not.toContain('interactive');
  });
  it('enforces safe mode in sanitized production projection, while preserving label', () => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml(`<a href="${path}?interactive=1">View original</a>`, {
      document, parser: null, sanitizer: DOMPurify, highlighter: null, mathRenderer: null,
      basePath: '/proxy/15000', projectActive: false,
    });
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/proxy/15000' + path);
    expect(root.textContent).toBe('View original');
  });
});
