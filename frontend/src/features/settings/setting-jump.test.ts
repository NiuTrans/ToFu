import { describe, expect, it } from 'vitest';

/* Contract between the jump registries and generated delivery markup: every
 * setting key must land on a real control of a real settings tab (panels are
 * generated — lib/settings_panels.py → static/settings_panels/ — so a renamed
 * id breaks jumps silently without this pin), and every page key must name a
 * routed entry point. `?raw` imports keep the check inside the vite graph —
 * node builtins are untyped under tsconfig.vite. */
import indexShell from '../../../../index.html?raw';
import advancedPanel from '../../../../static/settings_panels/advanced.html?raw';
import generalPanel from '../../../../static/settings_panels/general.html?raw';
import presetPanel from '../../../../static/settings_panels/preset.html?raw';
import searchPanel from '../../../../static/settings_panels/search.html?raw';
import speechPanel from '../../../../static/settings_panels/speech.html?raw';
import translatePanel from '../../../../static/settings_panels/translate.html?raw';
import featureBridge from '../../runtime/sections/feature-bridge.js?raw';
import mainEntry from '../../main.ts?raw';
import zhLocale from '../../i18n/locales/zh.json?raw';

import {
  SETTING_JUMP_TARGETS,
  resolveSettingJumpTarget,
} from './setting-jump';
import {
  PAGE_JUMP_TARGETS,
  resolvePageJumpTarget,
} from '../ui-nav';

const PANELS_BY_TAB: Readonly<Record<string, string>> = Object.freeze({
  advanced: advancedPanel,
  general: generalPanel,
  preset: presetPanel,
  search: searchPanel,
  speech: speechPanel,
  translate: translatePanel,
});

const KEBAB_CASE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

describe('SETTING_JUMP_TARGETS', () => {
  it('uses kebab-case keys (they appear in assistant-authored links)', () => {
    for (const key of Object.keys(SETTING_JUMP_TARGETS)) {
      expect(key).toMatch(KEBAB_CASE);
    }
  });

  it('points every control-level key at an element id inside its tab panel', () => {
    for (const [key, target] of Object.entries(SETTING_JUMP_TARGETS)) {
      if (!target.elementId) continue; // tab-level target: tab switch is the landing
      const panel = PANELS_BY_TAB[target.tab];
      expect(panel, `${key} → panel ${target.tab}`).toBeTruthy();
      expect(panel, `${key} → #${target.elementId}`).toContain(`id="${target.elementId}"`);
    }
  });

  it('points every key at a tab the settings shell exposes', () => {
    for (const [key, target] of Object.entries(SETTING_JUMP_TARGETS)) {
      expect(indexShell, `${key} → tab ${target.tab}`).toContain(`data-tab="${target.tab}"`);
    }
  });

  it('never maps two control-level keys onto the same control', () => {
    const elementIds = Object.values(SETTING_JUMP_TARGETS)
      .map((target) => target.elementId)
      .filter((elementId): elementId is string => Boolean(elementId));
    expect(new Set(elementIds).size).toBe(elementIds.length);
  });

  it('offers a tab-level target for every settings tab', () => {
    const tabs = Object.values(SETTING_JUMP_TARGETS).map((target) => target.tab);
    for (const tab of new Set(tabs)) {
      const hasTabTarget = Object.entries(SETTING_JUMP_TARGETS)
        .some(([, target]) => target.tab === tab && !target.elementId);
      expect(hasTabTarget, `tab-level target for ${tab}`).toBe(true);
    }
  });
});

describe('resolveSettingJumpTarget', () => {
  it('resolves a registered key', () => {
    expect(resolveSettingJumpTarget('debug-mode'))
      .toEqual({ tab: 'general', elementId: 'settingDebugMode' });
  });

  it('rejects unknown, empty, and non-string keys', () => {
    expect(resolveSettingJumpTarget('no-such-setting')).toBeNull();
    expect(resolveSettingJumpTarget('')).toBeNull();
    expect(resolveSettingJumpTarget(undefined)).toBeNull();
    expect(resolveSettingJumpTarget(42)).toBeNull();
  });
});

describe('PAGE_JUMP_TARGETS', () => {
  it('uses kebab-case keys (they appear in assistant-authored links)', () => {
    for (const key of Object.keys(PAGE_JUMP_TARGETS)) {
      expect(key).toMatch(KEBAB_CASE);
    }
  });

  it('names a routed entry point for every page key', () => {
    /* Entries resolve at click time through the module bridge: a feature-
     * bridge stub (feature-bridge.js) or a registered action (main.ts). */
    for (const [key, target] of Object.entries(PAGE_JUMP_TARGETS)) {
      const wired = featureBridge.includes(`'${target.entry}'`)
        || mainEntry.includes(`'${target.entry}'`);
      expect(wired, `${key} → entry ${target.entry}`).toBe(true);
    }
  });

  it('points every page name at an existing i18n key', () => {
    for (const [key, target] of Object.entries(PAGE_JUMP_TARGETS)) {
      expect(zhLocale, `${key} → nameKey ${target.nameKey}`)
        .toContain(`"${target.nameKey}"`);
    }
  });
});

describe('resolvePageJumpTarget', () => {
  it('resolves a registered key', () => {
    expect(resolvePageJumpTarget('memory'))
      .toEqual({ entry: 'openMemoryModal', nameKey: 'memory.title' });
  });

  it('rejects unknown, empty, and non-string keys', () => {
    expect(resolvePageJumpTarget('no-such-page')).toBeNull();
    expect(resolvePageJumpTarget('')).toBeNull();
    expect(resolvePageJumpTarget(undefined)).toBeNull();
    expect(resolvePageJumpTarget(42)).toBeNull();
  });
});
