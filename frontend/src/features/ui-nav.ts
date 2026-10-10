/* ═══════════════════════════════════════════════════════════════════
   features/ui-nav.ts — page-level "jump to sub-page" deep-link owner.

   Problem: like settings (see settings/setting-jump.ts), users ask the
   assistant to take them to a Tofu sub-page (memory, orchestration, …).
   Decision: the assistant emits a markdown link `[label](page:<key>)`; the
   markdown pipeline renders it as a button with data-tofu-action=
   "jumpToPage('<key>')" and this module owns the click. Every target entry
   point is already a lazy routed stub (feature-bridge) or registered action,
   so this module is a tiny eager registry — jumpToPage is wired in main.ts,
   not in a lazy domain.
   Alternatives considered: a single `setting:` scheme for everything
   (rejected — pages are not settings tabs; separate schemes keep the
   model-facing directory self-explanatory); toggles as entries (rejected —
   entries must be open-idempotent, e.g. openMemoryModal, never toggleMemory).

   The contract generator scripts/gen_ui_nav_targets.mjs serializes
   PAGE_JUMP_TARGETS (with setting-jump.ts's registry) into
   contracts/ui_nav_targets_v1.json for the model-facing context block.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../feature-registry';
import { t, type I18nKey } from '../i18n';

export interface PageJumpTarget {
  /** Routed feature entry point (feature-bridge stub or registered action)
   * that opens the page. MUST be open-idempotent — never a close-toggle. */
  readonly entry: string;
  /** i18n key naming the page in the model-facing directory. */
  readonly nameKey: I18nKey;
  /** Feature flag (GET /api/v1/features) gating the entry point. */
  readonly requiresFlag?: string;
}

export const PAGE_JUMP_TARGETS: Readonly<Record<string, PageJumpTarget>> = Object.freeze({
  'memory': { entry: 'openMemoryModal', nameKey: 'memory.title' },
  'orchestration': {
    entry: 'openOrchestration',
    nameKey: 'orch.shell.title',
    requiresFlag: 'debug_mode',
  },
  'project-brain': { entry: 'openProjectBrain', nameKey: 'projectBrain.title' },
  'knowledge-base': { entry: 'openKnowledgeBase', nameKey: 'knowledge.title' },
  'daily-report': { entry: 'openDailyReport', nameKey: 'myday.title' },
  'evolution': { entry: 'openEvolution', nameKey: 'evolution.studio.title' },
  'update': { entry: 'openUpdateDialog', nameKey: 'update.title' },
});

type UiNavBridge = {
  showToast?: (message: string, kind?: string) => void;
  _featureFlags?: Record<string, unknown>;
};

export function resolvePageJumpTarget(key: unknown): PageJumpTarget | null {
  if (typeof key !== 'string' || !key) return null;
  return PAGE_JUMP_TARGETS[key] ?? null;
}

export function jumpToPage(key: unknown): void {
  const bridge = featureRegistry as unknown as UiNavBridge;
  const target = resolvePageJumpTarget(key);
  const unavailable = (): void => {
    bridge.showToast?.(t('uiNav.jumpUnavailable'), 'error');
  };
  if (!target) {
    unavailable();
    return;
  }
  if (target.requiresFlag && !bridge._featureFlags?.[target.requiresFlag]) {
    /* Flag-gated page (e.g. orchestration needs debug_mode): the model only
     * advertises it when the directory says so, but flags change — fail
     * closed with feedback instead of a dead button. */
    unavailable();
    return;
  }
  /* The full resolver chain (registered action → feature binding → runtime
   * stub) lives behind the public module bridge; entry stubs lazy-load
   * their own domain chunk. */
  const open = window.TofuModules?.resolveAction?.(target.entry);
  if (typeof open !== 'function') {
    unavailable();
    return;
  }
  open();
}
