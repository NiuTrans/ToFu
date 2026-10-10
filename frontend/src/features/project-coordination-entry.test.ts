// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import source from '../runtime/sections/presence.js?raw';
import { createButton } from '../ui';
import { createPresenceSummaryController } from './presence-summary-controller';

it('keeps one factory entrance even for an empty project, with no new boot timer', async () => {
  document.body.innerHTML = '<div id="presenceStrip" hidden></div>';
  const scope: Record<string, any> = {};
  const open = vi.fn(async (_ports: unknown) => {});
  const getActiveConv = () => ({ id: 'worker', projectPath: '/project' });
  const interval = vi.spyOn(globalThis, 'setInterval').mockImplementation(() => { throw new Error('no boot interval'); });
  try {
    new Function('runtimeScope', 'pushSubscribe', 'createPresenceSummaryController',
      'createProjectCoordinationButton', 'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 't', source)(
      scope, () => {}, createPresenceSummaryController, createButton, open, getActiveConv,
      { project: { brainSummary: async () => null } }, (key: string) => key,
    );
    await Promise.resolve();
    const host = document.getElementById('presenceStrip')!;
    expect(host.hidden).toBe(false);
    expect(host.querySelectorAll('button')).toHaveLength(1);
    host.querySelector('button')!.click();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][0]).toMatchObject({ path: '/project', currentConversationId: 'worker' });
    scope.CollabBar._setSummary('/project', { status: { activeCount: 2, recentOutcomeCount: 3 } });
    scope.CollabBar._render();
    expect(host.querySelectorAll('button')).toHaveLength(1);
    expect(host.textContent).toContain('2');
    expect(host.textContent).toContain('3');
  } finally { interval.mockRestore(); }
});
