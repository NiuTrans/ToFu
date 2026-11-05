import {beforeEach, describe, expect, it, vi} from 'vitest';

const {registry, session} = vi.hoisted(() => ({
  registry: {} as Record<string, unknown>,
  session: {current: null as null | {isCurrent(): boolean}},
}));

vi.mock('../../feature-registry', () => ({featureRegistry: registry}));
vi.mock('./panel-read-session', () => ({currentSettingsReadSession: () => session.current}));
vi.mock('../../runtime/knowledge-presenters.generated.js', () => ({}));
vi.mock('../../i18n', () => ({t: (key: string) => key}));

import {
  deactivateKnowledgeSettingsTab,
  destroyKnowledgeSettingsTab,
  populateKnowledgeSettingsTab,
} from './knowledge-tab';

describe('Knowledge Settings lifecycle', () => {
  let active: boolean;

  beforeEach(() => {
    active = true;
    session.current = {isCurrent: () => true};
    vi.stubGlobal('document', {getElementById: (id: string) => id === 'settingsTab_knowledge'
      ? {classList: {contains: (name: string) => name === 'active' && active}}
      : null});
  });

  it('opens the workbench only for a current visit and closes it on departure', async () => {
    const calls: string[] = [];
    registry.openKnowledgeBase = () => calls.push('open');
    registry.closeKnowledgeBase = () => calls.push('close');

    await populateKnowledgeSettingsTab();
    expect(calls).toEqual(['open']);

    active = false;
    deactivateKnowledgeSettingsTab('general');
    expect(calls).toEqual(['open', 'close']);

    active = true;
    await populateKnowledgeSettingsTab();
    expect(calls).toEqual(['open', 'close', 'open']);
    destroyKnowledgeSettingsTab();
    expect(calls).toEqual(['open', 'close', 'open', 'close']);
  });

  it('does not open a retired Settings visit after the lazy presenter arrives', async () => {
    const calls: string[] = [];
    registry.openKnowledgeBase = () => calls.push('open');
    registry.closeKnowledgeBase = () => calls.push('close');
    session.current = {isCurrent: () => false};

    await populateKnowledgeSettingsTab();
    expect(calls).toEqual(['close']);
  });
});
