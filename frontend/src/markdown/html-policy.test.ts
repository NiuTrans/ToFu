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
