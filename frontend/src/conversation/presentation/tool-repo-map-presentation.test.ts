import { describe, expect, it, vi } from 'vitest';
import { REPO_MAP_HEADER_LIMITS, renderRepoMapHeaderHtml } from './tool-repo-map-presentation';
import type { Translator } from '../../i18n';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');
const messages: Record<string, string> = {
  'tool.repoMap.label': 'Repo Map',
  'tool.repoMap.description': 'Project files, functions and classes',
};
const translate: Translator = (key, ..._args) => messages[key] || key;
function inspect(round: unknown, check: (document: Document) => void): void {
  const dom = new JSDOM(`<body>${renderRepoMapHeaderHtml(round, translate)}</body>`);
  try { check(dom.window.document); } finally { dom.window.close(); }
}
const round = { toolName: 'repo_map', query: 'repo_map lib' };

describe('repo-map requested scope', () => {
  it.each(['object', 'JSON'] as const)('shows all supplied parameters from %s without mutation', format => {
    const args = Object.freeze({ path: 'lib', focus: 'swarm', max_tokens: 4500 });
    const value = Object.freeze({ ...round, toolArgs: format === 'JSON' ? JSON.stringify(args) : args });
    const before = JSON.stringify(value);
    inspect(value, document => {
      expect(document.querySelector('.ptool-repo-map-params')?.textContent)
        .toBe('path: lib · focus: swarm · max_tokens: 4500');
      expect(document.querySelectorAll('code')).toHaveLength(3);
    });
    expect(JSON.stringify(value)).toBe(before);
  });

  it('distinguishes explicitly empty strings and never invents omitted defaults', () => {
    inspect({ ...round, toolArgs: { path: '', focus: '' } }, document => {
      expect(document.querySelector('.ptool-repo-map-params')?.textContent).toBe('path: "" · focus: ""');
      expect(document.body.textContent).not.toContain('max_tokens');
    });
    inspect({ ...round, toolArgs: {} }, document => {
      expect(document.querySelector('.ptool-repo-map-params')).toBeNull();
    });
  });

  it.each([undefined, null, '{broken', 'null', '[]', 'x'.repeat(REPO_MAP_HEADER_LIMITS.argumentUnits + 1)])(
    'keeps a useful title when historical arguments are missing or invalid', toolArgs => {
      inspect({ ...round, query: 'repo_map legacy/scope', toolArgs }, document => {
        expect(document.querySelector('.ptool-repo-map-params')?.textContent).toBe('repo_map legacy/scope');
        expect(document.body.textContent).not.toContain('2048');
      });
    },
  );

  it('escapes hostile values and visibly bounds strings', () => {
    const hostile = '<img src=x onerror="unsafe()"> & <script>unsafe()</script>';
    inspect({ ...round, toolArgs: { path: hostile, focus: 'x'.repeat(4096), max_tokens: 4500 } }, document => {
      const values = document.querySelectorAll('code');
      expect(values[0].textContent).toBe(hostile);
      expect(values[1].textContent).toBe('x'.repeat(REPO_MAP_HEADER_LIMITS.valueUnits) + '…');
      expect(document.querySelector('img, script')).toBeNull();
    });
  });

  it.each([0, -1, 1.5, Infinity, NaN, '4500', {}])('does not turn invalid numeric scope into a claim', max_tokens => {
    inspect({ ...round, toolArgs: { path: 3, focus: false, max_tokens } }, document => {
      expect(document.querySelector('.ptool-repo-map-params')).toBeNull();
    });
  });

  it('ignores inherited scope and does not intercept other tools', () => {
    inspect({ ...round, toolArgs: Object.create({ path: 'foreign', focus: 'foreign', max_tokens: 4500 }) }, document => {
      expect(document.querySelector('.ptool-repo-map-params')).toBeNull();
    });
    expect(renderRepoMapHeaderHtml({ toolName: 'read_files', toolArgs: { path: 'lib' } }, translate)).toBe('');
  });
});
