/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let panel: typeof import('./panel');
let registry: Record<string, unknown>;
const calls = { list: vi.fn(), toggle: vi.fn(), setScope: vi.fn(), install: vi.fn() };
const projects = [{ path: '/work/tofu' }, { path: '/work/autoharness' }];
const skill = (path: string, ref: string) => ({
  id: 'shared-guide', name: 'Shared guide', is_package: true, enabled: true,
  scope: 'project', project_path: path, skill_ref: ref,
  source_path: `${path}/.agents/skills/shared-guide`, description: 'A workflow',
});

beforeEach(async () => {
  vi.resetModules();
  vi.clearAllMocks();
  document.body.innerHTML = '<section id="settingsTab_skills"><div id="skillsCategoryBar"></div><div id="skillsCatalogGrid"></div></section>';
  registry = (await import('../../feature-registry')).featureRegistry;
  calls.list.mockImplementation(async (_scope, path) => ({
    skills: path ? [skill(path, path.endsWith('tofu') ? 'tofu-ref' : 'harness-ref')] : [],
    destinations: { global: '/data/skills/users/7', project: path ? `${path}/.tofu/skills` : null },
  }));
  calls.toggle.mockResolvedValue({ ok: true, json: async () => ({}) });
  calls.setScope.mockResolvedValue({ ok: true, json: async () => ({}) });
  registry.Api = {
    project: { recentList: async () => ({ projects }) },
    skills: { ...calls, catalog: async () => ({ catalog: [] }) },
  };
  registry.t = (key: string, values?: Record<string, unknown>) => `${key} ${Object.values(values || {}).join(' | ')}`;
  registry._ephemeralToast = vi.fn();
  registry.showConfirm = vi.fn(async () => true);
  registry.showChoice = vi.fn(async () => '/work/autoharness');
  panel = await import('./panel');
  await panel.populateSkillsTab();
  panel.setScope('installed');
});
afterEach(() => panel.destroySkills());

describe('skills across projects', () => {
  it('lists external projects, retains duplicate names and filters by source', () => {
    expect(calls.list.mock.calls.map((args) => args[1])).toEqual(['', '/work/tofu', '/work/autoharness']);
    expect(document.querySelectorAll('.skill-card')).toHaveLength(2);
    expect(document.body.textContent).toContain('/work/autoharness/.agents/skills/shared-guide');
    const selector = document.querySelector<HTMLSelectElement>('#skillsProjectFilter')!;
    selector.value = '/work/autoharness';
    selector.dispatchEvent(new Event('change', { bubbles: true }));
    expect(document.querySelectorAll('.skill-card')).toHaveLength(1);
    expect(document.querySelector('[data-skills-action="move"]')?.getAttribute('data-skill-id')).toBe('harness-ref');
  });

  it('toggles the chosen source instead of the other project with the same id', async () => {
    await panel.toggleEnabled('harness-ref');
    expect(calls.toggle).toHaveBeenCalledWith('shared-guide', {
      project_path: '/work/autoharness', skill_ref: 'harness-ref',
    });
  });

  it('previews source, destination and filesystem semantics before moving into the global library', async () => {
    await panel.moveScope('tofu-ref', 'project');
    expect(registry.showChoice).not.toHaveBeenCalled();
    expect(registry.showConfirm).toHaveBeenCalledWith(expect.stringContaining(
      '/work/tofu/.agents/skills/shared-guide | /data/skills/users/7/shared-guide'), { danger: true });
    expect(calls.setScope).toHaveBeenCalledWith('shared-guide', 'global', {
      project_path: '/work/tofu', skill_ref: 'tofu-ref',
    }, '');
  });

  it('never offers a project-only destination (skills are owner-global)', async () => {
    const { chooseSkillDestination } = await import('./library');
    await expect(chooseSkillDestination('any skill')).resolves.toEqual({ scope: 'global', projectPath: '' });
    expect(registry.showChoice).not.toHaveBeenCalled();
  });

  it('cancellation and panel teardown never move files', async () => {
    registry.showConfirm = vi.fn(async () => false);
    await panel.moveScope('tofu-ref', 'project');
    expect(calls.setScope).not.toHaveBeenCalled();
    registry.showConfirm = async () => { panel.destroySkills(); return true; };
    await panel.moveScope('tofu-ref', 'project');
    expect(calls.setScope).not.toHaveBeenCalled();
  });

  it('shows partial source failures without losing the other projects', async () => {
    calls.list.mockImplementation(async (_scope, path) => {
      if (path === '/work/tofu') throw new Error('offline');
      return { skills: path ? [skill(path, 'harness-ref')] : [] };
    });
    await panel.populateSkillsTab();
    expect(document.querySelector('.skills-library-warning')?.textContent).toContain('/work/tofu');
    expect(document.querySelectorAll('.skill-card')).toHaveLength(1);
  });
});
