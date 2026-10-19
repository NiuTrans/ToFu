// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import DOMPurify from 'dompurify';
import { projectMarkdownHtml } from './html-policy';
import { markdownImagePreviewSource } from './images';
import { createConversationSurface } from '../conversation/ui/conversation-surface';
import type { MarkdownRenderEnvironment } from './ports';

const environment: MarkdownRenderEnvironment = {
  document, parser: null, sanitizer: DOMPurify, highlighter: null,
  mathRenderer: null, basePath: '/proxy/15000', projectActive: false,
  text: (key, fallback) => key === 'markdown.previewImage' ? '放大图片' : fallback,
};
function project(source: string) {
  const host = document.createElement('div');
  host.innerHTML = projectMarkdownHtml(source, environment);
  return host;
}
afterEach(() => document.body.replaceChildren());

describe('Markdown image projection', () => {
  it('marks published/proxy images with localized preview while preserving encoded URLs and dimensions', () => {
    const host = project('<p><img src="/api/response-assets/a%20b?x=1&amp;y=2" alt="A &amp; B" width="640" height="480"></p>');
    const image = host.querySelector('img')!;
    expect(image.getAttribute('src')).toBe('/proxy/15000/api/response-assets/a%20b?x=1&y=2');
    expect(image.getAttribute('width')).toBe('640');
    expect(image.getAttribute('height')).toBe('480');
    expect(image.classList.contains('md-preview-image')).toBe(true);
    expect(image.getAttribute('role')).toBe('button');
    expect(image.tabIndex).toBe(0);
    expect(image.getAttribute('aria-label')).toBe('放大图片: A & B');
    expect(host.querySelector('p')!.classList.contains('md-image-only')).toBe(true);
  });
  it('retains mixed prose, formatting, image groups and linked navigation', () => {
    const host = project('<p>Before <img src="/api/images/a"> after <strong>bold</strong></p>'
      + '<p> <img src="/api/images/b"><br><img src="/api/images/c"> </p>'
      + '<p><a href="https://example.org"><img src="/api/images/d"></a></p><p><br> </p>');
    const paragraphs = host.querySelectorAll('p');
    expect(paragraphs[0].classList.contains('md-image-only')).toBe(false);
    expect(paragraphs[0].textContent).toBe('Before  after bold');
    expect(paragraphs[1].classList.contains('md-image-only')).toBe(true);
    expect(paragraphs[2].classList.contains('md-image-only')).toBe(true);
    expect(paragraphs[3].classList.contains('md-image-only')).toBe(false);
    const linked = host.querySelector('a img')!;
    expect(linked.classList.contains('md-preview-image')).toBe(false);
    expect(linked.hasAttribute('role')).toBe(false);
    expect(linked.hasAttribute('tabindex')).toBe(false);
    expect(host.querySelector('a')!.getAttribute('href')).toBe('https://example.org');
  });
  it('never turns inaccessible, empty, executable or SVG-data sources into preview controls', () => {
    const host = project('<p><img src="/workspace/private.png"></p>'
      + '<img src="javascript:alert(1)" class="md-preview-image" data-preview-src="https://example.org" tabindex="0">'
      + '<img src=""><img src="data:image/svg+xml;base64,PHN2Zz4=">');
    expect(host.querySelector('.md-inaccessible-attachment')).not.toBeNull();
    expect(host.querySelector('.md-preview-image')).toBeNull();
    expect(host.querySelector('[data-preview-src]')).toBeNull();
  });
  it('uses the actual source instead of authored preview metadata', () => {
    const host = project('<img src="/api/images/a" data-preview-src="javascript:bad" data-conversation-action="bad">');
    const image = host.querySelector('img')!;
    expect(image.hasAttribute('data-preview-src')).toBe(false);
    expect(image.hasAttribute('data-conversation-action')).toBe(false);
    expect(markdownImagePreviewSource(image)).toBe('/proxy/15000/api/images/a');
    image.removeAttribute('src');
    expect(markdownImagePreviewSource(image)).toBe('');
  });
});

describe('ConversationSurface Markdown previews', () => {
  it('delegates pointer/Enter/Space, revalidates changes, preserves links and releases keyboard listeners', () => {
    const host = document.createElement('main');
    document.body.append(host);
    const open = vi.fn();
    const surface = createConversationSurface(host, { onPreviewImage: open });
    const content = document.createElement('div'); content.className = 'md-content';
    content.innerHTML = projectMarkdownHtml('<p><img src="/api/images/a"></p>', environment);
    surface.root.append(content);
    const image = content.querySelector('img')!;
    image.click();
    for (const key of ['Enter', ' ']) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
      image.dispatchEvent(event); expect(event.defaultPrevented).toBe(true);
    }
    expect(open).toHaveBeenCalledTimes(3);
    image.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true }));
    expect(open).toHaveBeenCalledTimes(3);
    image.src = '/api/images/new'; image.dataset.previewSrc = 'javascript:bad'; image.click();
    expect(open).toHaveBeenLastCalledWith('/api/images/new');
    image.src = 'javascript:bad';
    const unsafe = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    image.dispatchEvent(unsafe); expect(unsafe.defaultPrevented).toBe(false);
    expect(open).toHaveBeenCalledTimes(4);
    image.src = '/api/images/new';
    const link = document.createElement('a'); link.href = '#linked'; image.replaceWith(link); link.append(image);
    image.click(); image.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(open).toHaveBeenCalledTimes(4);
    link.replaceWith(image);
    surface.dispose(); host.append(surface.root);
    image.click(); image.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(open).toHaveBeenCalledTimes(4);
  });
});
