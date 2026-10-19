// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  captureConversationProject, createConversationProjectController,
  noteConversationProjectChoice, projectConversationWorkspace,
  projectStatusMatchesWorkspace, protectHydratingProjectSettings,
  withProjectHydration,
} from './conversation-project';
import { buildConversationSettingsSnapshot, createConversationSettingsResolution } from './conversation-settings-resolution';
import { paintOptionalProjectAttachment } from '../ui/optional-project-attachment';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const a = () => ({ id: 'a', projectPath: '/a', projectPaths: ['/a', '/a/lib'], readOnlyPaths: ['/a/lib'] });
const b = () => ({ id: 'b', projectPath: '/b', projectPaths: ['/b'], readOnlyPaths: [] });
function harness() {
  let active: string | null = 'a';
  let selection = projectConversationWorkspace(null);
  const ids = new Set(['a', 'b']);
  const controller = createConversationProjectController({
    activeId: () => active, exists: id => ids.has(id),
    apply: value => { selection = value; },
  });
  return { controller, select: (id: string | null) => { active = id; },
    state: () => selection, remove: (id: string) => ids.delete(id) };
}

describe('conversation-owned project projection', () => {
  it('restores two conversations independently of a non-empty composer draft', () => {
    document.body.innerHTML = '<textarea id="userInput">unsent draft</textarea>';
    const h = harness();
    h.controller.restore(a());
    expect(h.state().path).toBe('/a');
    expect(h.state().extraRoots).toEqual([{ path: '/a/lib', readOnly: true }]);
    h.select('b'); h.controller.restore(b());
    expect(h.state().path).toBe('/b');
    h.select('a'); h.controller.restore(a());
    expect(h.state().path).toBe('/a');
    expect((document.getElementById('userInput') as HTMLTextAreaElement).value).toBe('unsent draft');
  });
  it('rejects late restore and delayed mutations after navigation, deletion, or another choice', async () => {
    const h = harness();
    const lateRestore = h.controller.begin();
    h.select('b'); h.controller.restore(b());
    h.controller.restore(a(), lateRestore);
    expect(h.state().path).toBe('/b');
    const stale = h.controller.beginMutation();
    h.controller.restore({ ...b(), projectPath: '/new' });
    await Promise.resolve();
    expect(stale()).toBe(false);
    const deleted = h.controller.beginMutation(); h.remove('b');
    expect(deleted()).toBe(false);
  });
  it('empty primary detaches stale extras and read-only roots', () => {
    const empty = { ...a(), projectPath: '' };
    expect(captureConversationProject(empty)).toEqual({ projectPath: '', projectPaths: [], readOnlyPaths: [] });
    expect(projectConversationWorkspace(empty).active).toBe(false);
    expect(captureConversationProject(null)).toEqual(captureConversationProject(empty));
  });
  it('global status can enrich exactly matching roots but not append foreign roots', () => {
    const selection = projectConversationWorkspace(a());
    expect(projectStatusMatchesWorkspace({ path: '/b' }, selection)).toBe(false);
    expect(projectStatusMatchesWorkspace({ path: '/a', extraRoots: ['/a/lib', '/foreign'] }, selection)).toBe(false);
    expect(projectStatusMatchesWorkspace({ path: '/a', extraRoots: ['/a/lib'] }, selection)).toBe(true);
    expect(projectStatusMatchesWorkspace({ path: '/ambient' }, projectConversationWorkspace(null))).toBe(false);
  });
  it('supports an explicit new-chat attachment but does not inherit it on navigation', () => {
    const h = harness(); h.select(null); h.controller.restore(null);
    h.controller.chooseDraft(a());
    expect(h.controller.captureDraft().projectPath).toBe('/a');
    h.select('b'); h.controller.restore(b());
    h.select(null); h.controller.restore(null);
    expect(h.controller.captureDraft().projectPath).toBe('');
  });
  it('preserves a new project choice against a late cold settings snapshot', async () => {
    const conv = a(); const pending = deferred<void>();
    const hydrate = withProjectHydration(conv, async () => {
      await pending.promise;
      Object.assign(conv, protectHydratingProjectSettings(conv, b()));
    });
    Object.assign(conv, { projectPath: '/chosen', projectPaths: ['/chosen'], readOnlyPaths: ['/chosen'] });
    noteConversationProjectChoice(conv);
    pending.resolve(); await hydrate;
    expect(captureConversationProject(conv)).toEqual({ projectPath: '/chosen', projectPaths: ['/chosen'], readOnlyPaths: ['/chosen'] });
    // The temporary fence is released: later authoritative updates still work.
    expect(protectHydratingProjectSettings(conv, b()).projectPath).toBe('/b');
  });
  it('releases failed hydrate fences and scopes choices to their own conversation', async () => {
    const convA = a(); const convB = b(); const pending = deferred<void>();
    const hydrate = withProjectHydration(convA, () => pending.promise);
    noteConversationProjectChoice(convA);
    expect(protectHydratingProjectSettings(convB, { projectPath: '/other' }).projectPath).toBe('/other');
    pending.reject(new Error('offline'));
    await expect(hydrate).rejects.toThrow('offline');
    expect(protectHydratingProjectSettings(convA, { projectPath: '/later' }).projectPath).toBe('/later');
  });
});

describe('new draft folder/default snapshot', () => {
  const empty = { projectPath: '', projectPaths: [], readOnlyPaths: [] };
  it('explicit clear stays private on first send instead of re-inheriting folder roots', async () => {
    const h = harness(); h.select(null);
    h.controller.startDraft({ folderId: 'folder-a', defaults: a() });
    h.controller.chooseDraft(empty);
    const conv = { id: 'new', ...h.controller.captureDraftIdentity() };
    const service = createConversationSettingsResolution({
      resolveConfig: async () => ({ projectPath: '/a', projectPaths: ['/a'], settings: a() }),
      resolveSettings: async () => a(), patchResolvedSettings: async () => ({ ok: true, status: 200 }),
      patchSettings: async () => ({ ok: true, status: 200 }),
    });
    const snapshot = buildConversationSettingsSnapshot(conv, '/ambient', 'en');
    const submission = await service.resolveSubmission({ conv_settings: snapshot }, { conv_settings: snapshot, overrides: {} });
    expect(conv).toMatchObject({ ...empty, folderId: 'folder-a' });
    expect(submission.config).toMatchObject({ ...empty, folderId: 'folder-a' });
    expect(submission.settings).toMatchObject({ ...empty, folderId: 'folder-a' });
  });
  it('A draft keeps folder A and read-only roots after the sidebar selects B and defaults change', () => {
    const h = harness(); h.select(null);
    let selectedFolder = 'folder-a';
    const defaults = a();
    h.controller.startDraft({ folderId: selectedFolder, defaults });
    selectedFolder = 'folder-b';
    defaults.projectPath = '/edited'; defaults.projectPaths.push('/edited');
    defaults.readOnlyPaths.length = 0;
    const send = h.controller.captureDraftIdentity();
    expect(selectedFolder).toBe('folder-b');
    expect(send).toEqual({ folderId: 'folder-a', ...captureConversationProject(a()) });
    send.projectPaths.push('/foreign'); send.readOnlyPaths.length = 0;
    expect(h.controller.captureDraftIdentity()).toEqual({ folderId: 'folder-a', ...captureConversationProject(a()) });
  });
  it('starts defaults with non-empty composer text without discarding user work', () => {
    const h = harness(); h.select(null);
    document.body.innerHTML = '<textarea id="userInput">unsent work</textarea>';
    h.controller.startDraft({ folderId: 'folder-a', defaults: a(), preserveExplicit: true });
    expect(h.state().path).toBe('/a');
    expect(h.state().extraRoots).toEqual([{ path: '/a/lib', readOnly: true }]);
    expect((document.getElementById('userInput') as HTMLTextAreaElement).value).toBe('unsent work');
  });
  it.each([a(), empty])('preserves explicit roots or explicit-private target when carrying unsent work', choice => {
    const h = harness(); h.select(null);
    h.controller.startDraft({ folderId: 'folder-a', defaults: b() });
    h.controller.chooseDraft(choice);
    h.controller.startDraft({ folderId: 'folder-b', defaults: b(), preserveExplicit: true });
    expect(h.controller.captureDraftIdentity()).toEqual({ folderId: 'folder-a', ...captureConversationProject(choice) });
    // An intentional fresh draft, unlike carrying work, gets new defaults.
    h.controller.startDraft({ folderId: 'folder-b', defaults: b() });
    expect(h.controller.captureDraftIdentity()).toEqual({ folderId: 'folder-b', ...captureConversationProject(b()) });
  });
  it('a new draft fences late restore/choices, while boot status repaint retains its snapshot', () => {
    const h = harness(); h.select(null);
    const lateRestore = h.controller.begin();
    const lateChoice = h.controller.beginMutation();
    h.controller.startDraft({ folderId: 'folder-a', defaults: a() });
    h.controller.restore(null, lateRestore);
    expect(lateChoice()).toBe(false);
    h.controller.restore(null);
    expect(h.state().path).toBe('/a');
    expect(h.controller.captureDraftIdentity().folderId).toBe('folder-a');
    h.controller.startDraft({ folderId: 'folder-b', defaults: b() });
    expect(h.controller.captureDraftIdentity()).toEqual({ folderId: 'folder-b', ...captureConversationProject(b()) });
  });
  it('moving an existing conversation or editing folder defaults does not rebind its workspace', () => {
    const h = harness(); const conv = { ...a(), folderId: 'folder-a' };
    h.controller.restore(conv);
    conv.folderId = 'folder-b';
    h.controller.startDraft({ folderId: 'folder-b', defaults: b() });
    h.controller.restore(conv);
    expect(captureConversationProject(conv)).toEqual(captureConversationProject(a()));
    expect(h.state().path).toBe('/a');
  });
});

describe('send workspace boundary', () => {

  it.each(['folder-a', null])('freezes captured folder identity (%s) together with roots before resolution', async folderId => {
    const pending = deferred<Record<string, unknown>>();
    const service = createConversationSettingsResolution({
      resolveConfig: () => pending.promise, resolveSettings: async () => ({ folderId: 'ambient' }),
      patchResolvedSettings: async () => ({ ok: true, status: 200 }), patchSettings: async () => ({ ok: true, status: 200 }),
    });
    const snapshot = buildConversationSettingsSnapshot({ ...a(), folderId }, '', 'en');
    const result = service.resolveSubmission({ conv_settings: snapshot }, { conv_settings: snapshot, overrides: {} });
    snapshot.folderId = 'folder-b'; Object.assign(snapshot, captureConversationProject(b()));
    pending.resolve({ folderId: 'ambient', settings: { folderId: 'ambient' } });
    const submission = await result;
    expect(submission.config).toMatchObject({ folderId, ...captureConversationProject(a()) });
    expect(submission.settings).toMatchObject({ folderId, ...captureConversationProject(a()) });
  });
  it('snapshot ignores the legacy ambient-path argument and normalizes Studio without changing Flow/model', () => {
    const source = { ...a(), chatMode: 'studio', model: 'model', activeFlow: 'flow', planMode: true };
    const settings = buildConversationSettingsSnapshot(source, '/ambient', 'en');
    expect(settings).toMatchObject({ projectPath: '/a', projectPaths: ['/a', '/a/lib'], readOnlyPaths: ['/a/lib'], chatMode: 'chat', model: 'model', activeFlow: 'flow', planMode: true });
  });
  it.each([a(), { ...b(), projectPath: '' }])('resolver never fills roots from ambient defaults ($id)', async conv => {
    const response = deferred<Record<string, unknown>>();
    const service = createConversationSettingsResolution({
      resolveConfig: () => response.promise,
      resolveSettings: async () => ({ projectPath: '/ambient' }),
      patchResolvedSettings: async () => ({ ok: true, status: 200 }), patchSettings: async () => ({ ok: true, status: 200 }),
    });
    const snapshot = buildConversationSettingsSnapshot(conv, '/ambient', 'en');
    const expected = captureConversationProject(conv);
    const submission = service.resolveSubmission({ conv_settings: snapshot }, { conv_settings: snapshot, overrides: {} });
    // Another conversation/project choice while resolution is delayed.
    Object.assign(snapshot, captureConversationProject({ projectPath: '/changed' }));
    response.resolve({ projectPath: '/ambient', projectPaths: ['/ambient'], settings: { projectPath: '/ambient' } });
    const result = await submission;
    expect(result.config).toMatchObject(expected);
    expect(result.settings).toMatchObject(expected);
  });
  it('legacy resolved-settings fallback cannot persist ambient roots', async () => {
    const empty = { conv_settings: { projectPath: '' }, overrides: {} };
    let persisted: Record<string, unknown> | undefined;
    const service = createConversationSettingsResolution({
      resolveConfig: async () => ({}),
      resolveSettings: async () => ({ projectPath: '/ambient', projectPaths: ['/ambient'] }),
      patchResolvedSettings: async () => ({ ok: false, status: 404 }),
      patchSettings: async (_, settings) => { persisted = settings; return { ok: true, status: 200 }; },
    });
    await service.persist('a', empty);
    expect(persisted).toMatchObject({ projectPath: '', projectPaths: [], readOnlyPaths: [] });
  });
  it('config-only background send freezes explicit empty roots before resolution', async () => {
    const pending = deferred<Record<string, unknown>>();
    const service = createConversationSettingsResolution({
      resolveConfig: () => pending.promise,
      resolveSettings: async () => ({}), patchResolvedSettings: async () => ({ ok: true, status: 200 }), patchSettings: async () => ({ ok: true, status: 200 }),
    });
    const conv = { ...a(), projectPath: '' };
    const expected = captureConversationProject(conv);
    const snapshot = buildConversationSettingsSnapshot(conv, '/ambient', 'en');
    const result = service.resolveConfig({ conv_settings: snapshot });
    Object.assign(snapshot, captureConversationProject(b()));
    pending.resolve({ projectPath: '/ambient', projectPaths: ['/ambient'], readOnlyPaths: ['/ambient'] });
    expect(await result).toMatchObject(expected);
  });
});

it('paints optional project actions instead of Chat/Studio picker without removing Flow/model controls', () => {
  document.body.innerHTML = `<button id="chatModeToggle" aria-haspopup="true" data-i18n-title="mobile.capabilityMode"><span id="chatModeLabel">Chat</span></button>
    <div id="chatModeMenu">Studio</div><button id="flowMenuToggle">Flow</button><button id="modelSelector">Model</button>
    <section><h3 class="mobile-sheet-section-title">Capabilities</h3>
    <div class="mobile-mode-item" data-mode="chat">Chat</div>
    <div id="mobileModeStudio" class="mobile-mode-item" data-mode="studio"><span class="mobile-sheet-item-name">Studio</span><span data-i18n="mobile.modeStudioDesc">Required project</span><span class="mobile-sheet-item-check"></span></div></section>`;
  const translate = (key: string) => key;
  paintOptionalProjectAttachment(document, translate);
  paintOptionalProjectAttachment(document, translate);
  expect(document.querySelector('#chatModeMenu')).toBeNull();
  expect(document.querySelector('.mobile-mode-item')).toBeNull();
  expect(document.getElementById('mobileModeStudio')?.textContent).not.toContain('Studio');
  expect(document.getElementById('chatModeToggle')?.getAttribute('aria-haspopup')).toBeNull();
  expect(document.getElementById('flowMenuToggle')).not.toBeNull();
  expect(document.getElementById('modelSelector')).not.toBeNull();
});
