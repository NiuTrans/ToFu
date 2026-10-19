// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import DOMPurify from 'dompurify';
import { explainWorkspaceLinks, isWorkspaceFileHref } from './workspace-links';
import { projectMarkdownHtml } from './html-policy';
import type { MarkdownRenderEnvironment } from './ports';

const environment: MarkdownRenderEnvironment = {
  document, parser: null, sanitizer: DOMPurify, highlighter: null, mathRenderer: null,
  basePath: '/proxy/15000', projectActive: false,
};
const workspace = ['designs/sidebar-coordinator/preview.html', 'README.md', 'docs/guide.md',
  '.worktrees/demo/designs/preview.html', './preview.html', '../docs/guide.md',
  '/mnt/work/preview.html', '/home/user/README.md', '/tmp/report', 'C:\\work\\README.md',
  '\\\\host\\share\\report.pdf', '~/report.md', 'file:///tmp/report.html',
  'filesystem:file:///tmp/report.html', '%2Eworktrees/demo/report.md', '%2Fhome/user/README.md',
  'file%3A%2F%2F%2Ftmp%2Freport.html', '.worktrees/demo/untyped-file',
  '/designs/sidebar-coordinator/preview.html', '/.worktrees/demo/preview.html', 'AGENTS.md'];
const web = ['https://example.org/docs/README.md', 'http://example.org/file.pdf', '//example.org/docs/a.md',
  '/docs/guide.md', '/api/artifacts/id/view', '/static/file.png', '/settings', '#guide', 'mailto:me@example.org',
  '/proxy/15000/docs/guide.md', 'guide', 'guide/next', './guide', '../guide', 'articles/intro.html',
  'guide.html', 'report.pdf', 'image.png', '/proxy/15000/designs/preview.html'];

describe('workspace file links are not browser delivery', () => {
  it.each(workspace)('classifies %s without resolving it', href => expect(isWorkspaceFileHref(href, environment.basePath)).toBe(true));
  it.each(web)('preserves web navigation %s', href => expect(isWorkspaceFileHref(href, environment.basePath)).toBe(false));
  it('escapes and preserves accessible label/source without a link, handler or fetch', () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml('<a href="file:///tmp/&lt;report&gt;.html" onclick="alert(1)"><strong>My report</strong><script>alert(1)</script></a>', environment);
    // Label is flattened into inert text; model markup cannot create controls.
    expect(root.querySelector('a,button,img,script,[onclick],[tabindex]')).toBeNull();
    expect(root.querySelector('[role=note]')?.textContent).toContain('My report');
    expect(root.querySelector('code')?.textContent).toBe('file:///tmp/<report>.html');
    expect(root.textContent).toContain('Unpublished workspace file');
    expect(fetch).not.toHaveBeenCalled(); fetch.mockRestore();
  });
  it('retains sanitizer and proxy policy for regular links and images', () => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml('<a href="/api/artifacts/id/view">Open</a><a href="#part">Part</a><a href="javascript:alert(1)">Unsafe</a><img src="/uploads/chart.png" alt="Chart">', environment);
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/proxy/15000/api/artifacts/id/view');
    expect(root.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(root.querySelectorAll('a')[1].getAttribute('target')).toBeNull();
    expect(root.querySelectorAll('a')[2].getAttribute('href')).toBeNull();
    expect(root.querySelector('img')?.getAttribute('src')).toBe('/proxy/15000/uploads/chart.png');
  });
  it('does not parse or allocate DOM for unchanged no-link output', () => {
    const allocate = vi.spyOn(document, 'createElement');
    const html = '<p>Plain &amp; formatted <strong>content</strong></p>';
    expect(explainWorkspaceLinks(html, document, '')).toBe(html);
    expect(allocate).not.toHaveBeenCalled(); allocate.mockRestore();
  });
  it('admits malformed-but-browser-valid anchor slash syntax for safety', () => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml('<a/href="file:///tmp/report.html">File</a>', environment);
    expect(root.querySelector('a')).toBeNull(); expect(root.textContent).toContain('Unpublished workspace file');
  });
  it('keeps source inert if the HTML document is unavailable', () => {
    const html = '<a href="./report.html">Report</a>';
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml(html, { ...environment, document: null });
    expect(root.querySelector('a')).toBeNull(); expect(root.textContent).toBe(html);
  });
  it('localizes the explanation without changing the accessible label/source', () => {
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml('<a href="README.md">Readme</a>', { ...environment,
      text: key => key === 'delivery.unpublished' ? '尚未发布的工作区文件' : '请先发布或上传此文件。' });
    expect(root.textContent).toContain('Readme — 尚未发布的工作区文件: README.md');
    expect(root.querySelector('a,button')).toBeNull();
  });
  it('remains escaped when sanitizer is missing', () => {
    const html = '<a href="file:///tmp/a.md">Report</a>';
    const root = document.createElement('div');
    root.innerHTML = projectMarkdownHtml(html, { ...environment, sanitizer: null });
    expect(root.querySelector('a,button')).toBeNull(); expect(root.textContent).toBe(html);
  });
});
