// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import DOMPurify from 'dompurify';
import { projectMarkdownHtml } from './html-policy';
import type { MarkdownRenderEnvironment } from './ports';

const environment: MarkdownRenderEnvironment = {
  document, parser: null, sanitizer: null, highlighter: null,
  mathRenderer: null, basePath: '', projectActive: false,
};

describe('HTML admission dependency failure', () => {
  it('leaves unsafe HTML and UI navigation inert without a sanitizer', () => {
    const source = '<script>alert(1)</script><img src=x onerror=alert(1)>'
      + '<svg onload=alert(1)></svg><a href="setting:api">Settings</a>';
    const host = document.createElement('div');
    host.innerHTML = projectMarkdownHtml(source, environment);
    expect(host.querySelector('script,img,svg,button,a')).toBeNull();
    expect(host.textContent).toBe(source);
  });
  it('uses supplied sanitizer and preserves normal sanitized formatting', () => {
    const sanitize = vi.fn((source: string) => DOMPurify.sanitize(source));
    const host = document.createElement('div');
    host.innerHTML = projectMarkdownHtml('<h2>Title</h2><script>alert(1)</script>',
      { ...environment, sanitizer: { sanitize } });
    expect(sanitize).toHaveBeenCalledOnce();
    expect(host.querySelector('h2')?.textContent).toBe('Title');
    expect(host.querySelector('script')).toBeNull();
  });
});

describe('post-sanitizer image admission', () => {
  const project = (source: string) => {
    const host = document.createElement('div');
    host.innerHTML = projectMarkdownHtml(source, { ...environment, sanitizer: DOMPurify });
    return host;
  };
  it('keeps markup inside a sanitized attribute value inert', () => {
    const alt = "x><img src='https://unreachable.invalid/p.png' onerror='alert(1)'><svg onload='alert(2)'>";
    const host = project(`<p><img alt="${alt}" src="https://example.com/a.png"></p>`);
    expect(host.querySelector('svg')).toBeNull();
    expect([...host.querySelectorAll('*')].flatMap(element => element.getAttributeNames())
      .filter(name => name.startsWith('on'))).toEqual([]);
    expect(host.querySelectorAll('img')).toHaveLength(1);
    expect(host.querySelector('img')!.getAttribute('src')).toBe('https://example.com/a.png');
    expect(host.querySelector('img')!.alt).toBe(alt);
  });
  it('admits by the actual src attribute, not text inside another attribute', () => {
    const host = project(`<img alt="A &amp; B src='https://example.com/ok.png'" src="/workspace/secret.png">`);
    expect(host.querySelector('img')).toBeNull();
    expect(host.querySelector('.md-inaccessible-attachment')?.textContent)
      .toContain("Inaccessible attachment: A & B src='https://example.com/ok.png'.");
  });
});
