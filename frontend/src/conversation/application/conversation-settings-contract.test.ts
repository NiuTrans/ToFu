// @vitest-environment node
/** Browser half of contracts/conversation_settings_v1.json: the settings
 * writer, the retained adopter and the metadata cache match the contract, and
 * every composer choice survives a write → reload → write round trip. */
import { describe, expect, it, vi } from 'vitest';
import contract from '../../../../contracts/conversation_settings_v1.json';
import { CONVERSATION_CACHE_SETTING_KEYS } from '../../api/conversation-settings-contract';
import { extractConversationCacheSettings } from '../../core/conversation-metadata-cache';
import { buildConversationSettingsSnapshot } from './conversation-settings-resolution';

const { readFileSync } = await vi.importActual<{
  readFileSync(path: string, encoding: 'utf8'): string;
}>('node:fs');
const vm = await vi.importActual<{
  createContext(values: Record<string, unknown>): Record<string, unknown>;
  runInContext(source: string, context: unknown): unknown;
}>('node:vm');
const { cwd } = await vi.importActual<{ cwd(): string }>('node:process');

type Declaration = {
  author: string; adopt?: boolean; cache?: boolean; catalog?: boolean;
  clearWhenAbsent?: boolean; readAliases?: string[];
};
type Settings = Record<string, unknown>;
type Adopt = (conv: Settings, settings: unknown, projection?: 'full' | 'partial') => void;

const declarations = contract.keys as Record<string, Declaration>;
const declared = (test: (spec: Declaration) => boolean) =>
  Object.keys(declarations).filter(key => test(declarations[key])).sort();

function loadAdopter(): Adopt {
  const source = readFileSync(
    `${cwd()}/frontend/src/runtime/sections/core/conv_apply_settings.js`, 'utf8');
  const context = vm.createContext({
    protectHydratingProjectSettings: (_conv: unknown, settings: unknown) => settings,
  });
  vm.runInContext(source, context);
  return context._applySettingsToConv as Adopt;
}

const storedComposerChoices: Settings = {
  model: 'model-a', modelRef: { provider_id: 'provider-a', offering_id: 'offering-a' },
  preferredProviderId: 'provider-a', thinkingDepth: 'high', searchMode: 'single',
  fetchEnabled: false, codeExecEnabled: true, browserEnabled: true, desktopEnabled: true,
  memoryEnabled: false, schedulerEnabled: true, autopilotEnabled: true, activeFlow: 'review',
  imageGenEnabled: true, imageGenMode: true, imageGenModel: 'image-a',
  imageGenProviderId: 'image-provider', imageGenCount: 3, imageGenAspect: '16:9',
  imageGenResolution: '2K', humanGuidanceEnabled: true, planMode: true, delegateMode: true,
  projectPath: '/work', projectPaths: ['/work', '/work/lib'], readOnlyPaths: ['/work/lib'],
  autoTranslate: true, folderId: 'folder-a', autoApply: false,
};

describe('conversation settings contract (browser)', () => {
  it('sends exactly the browser-written vocabulary', () => {
    const snapshot = buildConversationSettingsSnapshot({}, '', 'en');

    expect(Object.keys(snapshot).sort()).toEqual(
      declared(spec => spec.author === 'composer' || spec.author === 'composer-derived'));
  });

  it('adopts exactly the declared keys and their read aliases', () => {
    const adopt = loadAdopter();
    const reads = new Set<string>();
    const recorder = new Proxy({}, {
      get(_target, key) {
        if (typeof key === 'string') reads.add(key);
        return undefined;
      },
    });

    adopt({}, recorder, 'full');
    adopt({}, recorder, 'partial');

    const aliases = Object.values(declarations).flatMap(spec => spec.readAliases ?? []);
    expect([...reads].sort()).toEqual(
      [...new Set([...declared(spec => spec.adopt === true), ...aliases])].sort());
  });

  it('caches exactly the declared cache keys, honouring read aliases', () => {
    const cacheKeys = declared(spec => spec.cache === true);

    expect([...CONVERSATION_CACHE_SETTING_KEYS].sort()).toEqual(cacheKeys);
    expect(Object.keys(extractConversationCacheSettings({})).sort()).toEqual(cacheKeys);
    expect(extractConversationCacheSettings({ preset: 'legacy-model' }).model).toBe('legacy-model');
  });

  it('restores every composer choice after a reload', () => {
    const adopt = loadAdopter();
    const composer = declared(spec => spec.author === 'composer');
    expect(Object.keys(storedComposerChoices).sort()).toEqual(composer);

    const reloaded: Settings = {};
    adopt(reloaded, storedComposerChoices, 'full');
    const rewritten = buildConversationSettingsSnapshot(reloaded, '', 'en') as Settings;

    for (const key of composer) {
      expect([key, rewritten[key]]).toEqual([key, storedComposerChoices[key]]);
    }
  });

  it('lets only full settings clear clearWhenAbsent state', () => {
    const adopt = loadAdopter();
    const cleared = declared(spec => spec.clearWhenAbsent === true);
    expect(cleared.length).toBeGreaterThan(0);

    for (const key of cleared) {
      const conv: Settings = {};
      adopt(conv, { [key]: { kept: true } }, 'full');
      adopt(conv, { folderId: 'folder-b' }, 'partial');
      expect(conv[key]).toEqual({ kept: true });
      expect(conv.folderId).toBe('folder-b');

      adopt(conv, {}, 'full');
      expect(conv[key]).toBeNull();
    }
  });

  it('keeps every adopted value when a partial projection omits it', () => {
    const adopt = loadAdopter();
    const conv: Settings = {};
    const everyAdopted: Settings = { ...storedComposerChoices };
    for (const key of declared(spec => spec.adopt === true)) {
      if (!(key in everyAdopted)) everyAdopted[key] = `${key}-value`;
    }
    adopt(conv, everyAdopted, 'full');
    const before = JSON.stringify(conv);

    adopt(conv, {}, 'partial');

    expect(JSON.stringify(conv)).toBe(before);
  });
});
