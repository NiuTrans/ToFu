/* ═══════════════════════════════════════════════════════════════════
   settings/setting-jump.ts — "jump to setting" deep-link owner.

   Problem: when the assistant explains a Tofu setting, the user still has to
   hunt through the settings modal to find the control.
   Decision: the assistant emits an ordinary markdown link whose href is
   `setting:<key>`; the markdown pipeline (runtime/sections/core/markdown.js)
   renders it as a button carrying data-tofu-action="jumpToSetting('<key>')"
   and this module owns the click — open Settings, activate the target tab,
   scroll the control into view and flash it. SETTING_JUMP_TARGETS is the
   single source of truth for valid keys; a future model-facing settings
   directory tool serializes this same map.
   Alternatives considered: raw HTML buttons in model output (rejected —
   DOMPurify strips them and they are injection-prone); a contracted payload
   in docs/contracts/EVENTS.md (deferred — heavier; revisit if jumps ever need server
   authority beyond the modal itself).

   Entry point: jumpToSetting (routed through feature-bridge → the settings
   domain in main.ts). Dependencies: openSettings/switchSettingsTab from
   settings/core-panel.ts, showToast from the retained runtime, i18n key
   uiNav.jumpUnavailable.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';

export interface SettingJumpTarget {
  /** Settings tab id — matches [data-tab] in the settings modal shell. */
  readonly tab: string;
  /** Element id of the control in static/settings_panels/<tab>.html.
   * Omitted for tab-level targets: the jump ends at the tab switch (dynamic
   * panel content like provider cards has no stable anchor). */
  readonly elementId?: string;
}

/* Canonical allowlist of jumpable settings. Keys are kebab-case (they appear
 * in assistant-authored links); elementIds are pinned to the generated panel
 * markup by setting-jump.test.ts. Only static controls qualify — dynamically
 * rendered content (provider cards, model catalog) has no stable anchor. */
export const SETTING_JUMP_TARGETS: Readonly<Record<string, SettingJumpTarget>> = Object.freeze({
  /* ── Tab-level targets ── */
  'general': { tab: 'general' },
  'preset': { tab: 'preset' },
  'api': { tab: 'api' },
  'models': { tab: 'models' },
  'oauth': { tab: 'oauth' },
  'search': { tab: 'search' },
  'translate': { tab: 'translate' },
  'speech': { tab: 'speech' },
  'network': { tab: 'network' },
  'devices': { tab: 'devices' },
  'preferences': { tab: 'preferences' },
  'skills': { tab: 'skills' },
  'tools': { tab: 'tools' },
  'mcp': { tab: 'mcp' },
  'experiments': { tab: 'experiments' },
  'advanced': { tab: 'advanced' },
  'feishu': { tab: 'feishu' },
  /* ── Control-level targets ── */
  'language': { tab: 'general', elementId: 'settingLanguage' },
  'system-prompt': { tab: 'general', elementId: 'settingSystem' },
  'default-thinking-depth': { tab: 'general', elementId: 'settingDefaultThinkingDepth' },
  'input-send-mode': { tab: 'general', elementId: 'settingInputSendMode' },
  'auto-generate-title': { tab: 'general', elementId: 'settingAutoGenerateTitle' },
  'debug-mode': { tab: 'general', elementId: 'settingDebugMode' },
  'optimizer': { tab: 'general', elementId: 'settingOptimizerEnabled' },
  'trading': { tab: 'general', elementId: 'settingTradingEnabled' },
  'pptx-translate': { tab: 'general', elementId: 'settingPptxTranslateEnabled' },
  'default-model': { tab: 'preset', elementId: 'settingDefaultModel' },
  'fallback-model': { tab: 'preset', elementId: 'settingFallbackModel' },
  'search-profile': { tab: 'search', elementId: 'settingSearchProfile' },
  'search-deepen': { tab: 'search', elementId: 'settingSearchDeepen' },
  'fetch-topn': { tab: 'search', elementId: 'settingFetchTopN' },
  'fetch-timeout': { tab: 'search', elementId: 'settingFetchTimeout' },
  'llm-content-filter': { tab: 'search', elementId: 'settingLlmContentFilter' },
  'skip-domains': { tab: 'search', elementId: 'settingSkipDomains' },
  'mt-enabled': { tab: 'translate', elementId: 'settingMtEnabled' },
  'mt-provider': { tab: 'translate', elementId: 'settingMtProvider' },
  'mt-api-url': { tab: 'translate', elementId: 'settingMtApiUrl' },
  'mt-api-key': { tab: 'translate', elementId: 'settingMtApiKey' },
  'mt-app-id': { tab: 'translate', elementId: 'settingMtAppId' },
  'stt-enabled': { tab: 'speech', elementId: 'settingSttEnabled' },
  'stt-provider': { tab: 'speech', elementId: 'settingSttProvider' },
  'tool-search': { tab: 'advanced', elementId: 'settingToolSearch' },
  'responses-reasoning': { tab: 'advanced', elementId: 'settingResponsesReasoning' },
  'responses-transport': { tab: 'advanced', elementId: 'settingResponsesTransport' },
  'responses-verbosity': { tab: 'advanced', elementId: 'settingResponsesVerbosity' },
  'programmatic-calling': { tab: 'advanced', elementId: 'settingProgrammaticCalling' },
  'pricing': { tab: 'advanced', elementId: 'settingPricing' },
});

const FLASH_CLASS = 'setting-jump-flash';
const FLASH_MS = 2600;
/* Toggles wrap their <input> in .settings-toggle-row, fields in
 * .settings-field — flash the whole row so the label participates. */
const ROW_SELECTOR = '.settings-toggle-row, .settings-field';

type SettingJumpBridge = {
  openSettings?: () => void;
  switchSettingsTab?: (tabId: string) => void;
  showToast?: (message: string, kind?: string) => void;
};

export function resolveSettingJumpTarget(key: unknown): SettingJumpTarget | null {
  if (typeof key !== 'string' || !key) return null;
  return SETTING_JUMP_TARGETS[key] ?? null;
}

export function jumpToSetting(key: unknown): void {
  const bridge = featureRegistry as unknown as SettingJumpBridge;
  const target = resolveSettingJumpTarget(key);
  if (!target) {
    /* Model hallucinated or renamed key: degrade to feedback, never a dead
     * button. The modal is left alone so an open editing session survives. */
    bridge.showToast?.(t('uiNav.jumpUnavailable'), 'error');
    return;
  }
  /* A full openSettings() re-populates every control from config — clobbering
   * unsaved edits — so only pay for it when the modal is actually closed. */
  const modal = document.getElementById('settingsModal');
  if (!modal?.classList.contains('open')) bridge.openSettings?.();
  bridge.switchSettingsTab?.(target.tab);
  /* Tab-level target: the tab switch IS the landing — panel content is
   * rendered on demand and has no stable anchor to flash. */
  if (!target.elementId) return;
  const elementId = target.elementId;
  /* The target panel is display:none until switchSettingsTab activates it;
   * the control is measurable only on the next frame. */
  requestAnimationFrame(() => {
    const element = document.getElementById(elementId);
    if (!element) {
      bridge.showToast?.(t('uiNav.jumpUnavailable'), 'error');
      return;
    }
    const row = (element.closest(ROW_SELECTOR) ?? element) as HTMLElement;
    /* Controls may live inside a collapsed <details class="settings-
     * disclosure"> (e.g. the 功能模块 group): a child of a closed details has
     * no box — it cannot be scrolled to, flashed, or focused. Reveal closed
     * ancestors first. */
    let details = element.closest('details');
    while (details) {
      if (!details.open) details.open = true;
      const parent = details.parentElement;
      details = parent ? parent.closest('details') : null;
    }
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    row.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
    row.classList.remove(FLASH_CLASS);
    /* Restart the flash animation when the same control is jumped to twice. */
    void row.offsetWidth;
    row.classList.add(FLASH_CLASS);
    window.setTimeout(() => row.classList.remove(FLASH_CLASS), FLASH_MS);
    /* Keyboard/screen-reader users land ON the control, not just near it.
     * Visually-hidden toggle inputs (.stg-toggle input is clipped to 1px)
     * refuse browser focus; fall back to the row so the jump still has a
     * focus landing announced by assistive tech. */
    if (typeof element.focus === 'function') {
      try { element.focus({ preventScroll: true }); } catch { element.focus(); }
      if (document.activeElement !== element) {
        if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', '-1');
        try { row.focus({ preventScroll: true }); } catch { row.focus(); }
      }
    }
  });
}
