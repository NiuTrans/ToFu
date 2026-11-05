// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  projectTurnContextRail,
  renderTurnContextChecklistRowHtml,
  renderTurnContextRailHtml,
  renderTurnContextRootBranchHtml,
  TURN_CONTEXT_PREVIEW_PATHS,
  TURN_CONTEXT_PREVIEW_TOOLS,
  type TurnContextRailHost,
} from '.';

const format = (fallback: string, values?: Readonly<Record<string, string | number>>): string => (
  fallback.replace(/\{(\w+)\}/g, (token, name: string) => (values && name in values ? String(values[name]) : token)));
const host: TurnContextRailHost = { text: (_key, fallback, values) => format(fallback, values) };

const tools = ['Search ×N', 'Fetch', 'Code Exec', 'Memory', 'Browser', 'MCP: overleaf ×21']
  .map((label) => ({ label, tone: label.startsWith('MCP') ? 'mcp' : 'net' }));

function mount(html: string): HTMLElement {
  const holder = document.createElement('div');
  holder.innerHTML = html;
  return holder.firstElementChild as HTMLElement;
}

describe('turn context rail', () => {
  it('renders every section with one heading anatomy and bounded previews', () => {
    const parts = renderTurnContextRailHtml({
      model: 'claude-opus-4-5', depth: 'max', modes: [{ label: 'Autopilot' }], tools,
      roots: ['a/b/one', 'two', 'three', { short: 'four', path: '/w/four', ro: true }],
      git: { branch: 'main' },
      todos: [{ content: 'plan', status: 'completed' }, { content: 'build', status: 'in_progress' },
        { content: 'ship', status: 'pending' }],
    }, { ...host, modelLabel: () => 'Claude Opus 4.5', brandIconHtml: () => '<span class="stg-brand-icon"><svg></svg></span>' });
    expect(parts).not.toBeNull();
    const rail = mount(parts!.rail);
    expect(rail.className).toBe('turn-ctx');
    expect(rail.querySelector('.tctx-head .tctx-logo .stg-brand-icon svg')).not.toBeNull();
    expect(rail.querySelector('.tctx-model-group .tctx-model')?.textContent).toBe('Claude Opus 4.5');
    expect(rail.querySelector('.tctx-model')?.getAttribute('title')).toBe('claude-opus-4-5');
    expect(rail.querySelector('.tctx-model-group .tctx-depth')?.textContent).toBe('max');
    expect(rail.querySelector('.tctx-mode-badge')?.textContent).toBe('Autopilot');
    const headings = [...rail.querySelectorAll('.tctx-row > .tctx-row-heading .tctx-row-h')]
      .map((node) => node.textContent);
    expect(headings).toEqual(['Tools6', 'Workspace4', 'Todos1/3']);

    const toolRow = rail.querySelector('.tctx-tools-row')!;
    expect(toolRow.querySelectorAll('.tctx-chips > .tctx-chip')).toHaveLength(TURN_CONTEXT_PREVIEW_TOOLS);
    expect(toolRow.querySelectorAll('.tctx-overflow[hidden] > .tctx-chip')).toHaveLength(2);
    const toggle = toolRow.querySelector('button[data-tctx-more]')!;
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toBe('Show 2 more');
    expect(toggle.parentElement?.className).toBe('tctx-row-heading');
    const server = toolRow.querySelector('.tctx-tone-mcp')!;
    expect(server.querySelector('.tctx-chip-label')?.textContent).toBe('overleaf ×21');
    expect(server.getAttribute('title')).toBe('MCP: overleaf ×21');
    expect([...toolRow.querySelectorAll('.tctx-chip')].every((chip) => chip.querySelector('.tctx-icon svg'))).toBe(true);

    const rootRow = rail.querySelector('.tctx-roots-row')!;
    expect(rootRow.querySelectorAll('.tctx-paths > .tctx-path')).toHaveLength(TURN_CONTEXT_PREVIEW_PATHS);
    const hiddenRoot = rootRow.querySelector('.tctx-overflow[hidden] > .tctx-path')!;
    expect(hiddenRoot.classList.contains('tctx-path--readonly')).toBe(true);
    expect(hiddenRoot.getAttribute('title')).toBe('/w/four');
    expect(rootRow.querySelector('.tctx-path')?.getAttribute('title')).toBe('a/b/one');

    // A branch follows its root after a colon, in the root's own text run;
    // a branch recorded without a path belongs to the primary root.
    expect([...rootRow.querySelectorAll('[data-tctx-root]')].map((line) => line.textContent))
      .toEqual(['b/one:main', 'two', 'three', 'four']);
    const suffix = rootRow.querySelector('[data-tctx-root="a/b/one"] > .tctx-path-text > [data-tctx-git]')!;
    expect(suffix.querySelector('.tctx-path-sep')?.textContent).toBe(':');
    expect(rail.querySelectorAll('[data-tctx-git]')).toHaveLength(1);
    const todo = rail.querySelector('[data-tctx-todo]')!;
    expect(todo.querySelector('.tctx-todo-current')?.textContent).toBe('build');
    expect([...todo.querySelectorAll('.tctx-todo-list .tctx-todo-item')].map((node) => node.textContent))
      .toEqual(['plan', 'ship']);
    expect((todo.querySelector('.tctx-todo-progress-fill') as HTMLElement).style.width).toBe('33%');

    const fold = mount(parts!.fold);
    expect(fold.className).toBe('tctx-fold');
    expect(fold.textContent).toBe('Claude Opus 4.5 · max · Autopilot · 6 tools · 4 workspaces · ⑂ main · 1/3 todos');
    expect(fold.getAttribute('title')).toBe(fold.textContent);
  });

  it('escapes persisted text and confines tones to class tokens', () => {
    const parts = renderTurnContextRailHtml({
      model: '<img src=x onerror=alert(1)>',
      tools: [{ label: '<b>x</b>', tone: 'net" onmouseover="alert(1)' }],
      roots: [{ short: '"><script>', path: '/p" onmouseover="alert(1)' }],
      git: { branch: '<img src=x onerror=alert(1)>' },
    }, host);
    const rail = mount(parts!.rail);
    expect(rail.querySelector('img, script, b')).toBeNull();
    expect(rail.querySelector('[onmouseover]')).toBeNull();
    expect(rail.querySelector('[data-tctx-git]')?.textContent).toBe(':<img src=x onerror=alert(1)>');
    expect(rail.querySelector('.tctx-chip')?.className).toBe('tctx-chip tctx-tone-tool');
    expect(rail.querySelector('.tctx-chip-label')?.textContent).toBe('<b>x</b>');
  });

  it('recovers legacy tool modes, string items and the live branch', () => {
    const model = projectTurnContextRail({
      tools: [{ label: 'Swarm', tone: 'mode' }, 'read_files', { label: 'Fetch', tone: 'net' }],
      roots: ['/very/long/absolute/path/project'],
    }, { currentGit: () => ({ branch: 'feature/rail' }) });
    expect(model?.modes).toEqual(['Swarm']);
    expect(model?.tools.map((tool) => tool.label)).toEqual(['read_files', 'Fetch']);
    expect(model?.roots[0]).toEqual({
      short: 'path/project', path: '/very/long/absolute/path/project', readOnly: false, branch: 'feature/rail' });
    expect(model?.git?.branch).toBe('feature/rail');
    // A recorded branch is history; the live branch never rewrites it.
    expect(projectTurnContextRail({ roots: ['/w/a'], git: { branch: 'main' } }, { currentGit: () => ({ branch: 'other' }) })
      ?.git?.branch).toBe('main');
    // A branch is probed from the roots: a turn without roots ran on none.
    const rootless = projectTurnContextRail({ model: 'm', git: { branch: 'main' } }, { currentGit: () => ({ branch: 'other' }) });
    expect(rootless?.modelId).toBe('m');
    expect(rootless?.git).toBeNull();
    expect(renderTurnContextRailHtml({}, host)).toBeNull();
    expect(renderTurnContextRailHtml(null, host)).toBeNull();
  });

  it("writes each root's own branch after it and keeps the live parts keyed", () => {
    const rail = mount(renderTurnContextRailHtml({
      roots: [{ short: 'a', path: '/w/a' }, { short: 'b', path: '/w/b', ro: true }, { short: 'c', path: '/w/c' }],
      git: { branch: 'main', branches: [
        { path: '/w/b', branch: 'dev' }, { path: '/w/a', branch: 'main' }, { path: '/w/elsewhere', branch: 'stray' }] },
    }, host)!.rail);
    // A root outside any repository shows no branch, and a branch of a path
    // the turn did not run in names no root.
    expect([...rail.querySelectorAll('[data-tctx-root]')].map((line) => line.textContent)).toEqual(['a:main', 'b:dev', 'c']);
    expect(rail.textContent).not.toContain('stray');
    // Each name is its own line-breaking unit; the colon sits between them.
    expect([...rail.querySelectorAll('[data-tctx-root="/w/a"] .tctx-path-name')].map((name) => name.textContent))
      .toEqual(['a', 'main']);

    const live = mount(renderTurnContextRootBranchHtml({ branch: 'feature', branches: [{ path: '/w/a', branch: 'feature' }] }, '/w/a', true));
    expect(live.className).toBe('tctx-path-branch');
    expect(live.dataset.tctxGit).toBe('1');
    expect(live.textContent).toBe(':feature');
    expect(live.lastElementChild?.className).toBe('tctx-path-name');
    // A branch recorded without a path belongs to the primary root only, and a
    // root without a path never takes another root's branch.
    expect(mount(renderTurnContextRootBranchHtml({ branch: 'main' }, '/w/a', true)).textContent).toBe(':main');
    expect(renderTurnContextRootBranchHtml({ branch: 'main' }, '/w/b', false)).toBe('');
    expect(renderTurnContextRootBranchHtml({ branch: 'main', branches: [{ path: '/w/a', branch: 'main' }] }, '', true)).toBe('');
    expect(renderTurnContextRootBranchHtml(null, '/w/a', true)).toBe('');

    const done = mount(renderTurnContextChecklistRowHtml([
      { content: 'one', status: 'completed' }, { content: 'two', status: 'completed' },
    ], host));
    expect(done.dataset.tctxTodo).toBe('1');
    expect(done.querySelector('.tctx-row-count')?.className).toBe('tctx-row-count is-done');
    expect(done.querySelector('.tctx-todo-current')?.textContent).toBe('two');
    const blocked = mount(renderTurnContextChecklistRowHtml([
      { content: 'wait', status: 'blocked' }, { content: 'later', status: 'unknown' },
    ], host));
    expect(blocked.querySelector('.tctx-todo-current')?.classList.contains('tctx-todo-blocked')).toBe(true);
    expect(blocked.querySelector('.tctx-todo-list .tctx-todo-pending')?.textContent).toBe('later');
    expect(renderTurnContextChecklistRowHtml([], host)).toBe('');
  });

  it('counts the whole plan while bounding the painted lines', () => {
    const todos = Array.from({ length: 30 }, (_, index) => ({ content: `step ${index}`, status: index < 3 ? 'completed' : 'pending' }));
    const row = mount(renderTurnContextChecklistRowHtml(todos, host));
    expect(row.querySelector('.tctx-row-count')?.textContent).toBe('3/30');
    expect(row.querySelectorAll('.tctx-todo-item')).toHaveLength(24);
  });
});
