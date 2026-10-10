import '../styles/application/09-slide-editor.css';
/** Demand-loaded editor composition; runtime supplies transport and composer ports only. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry, getFeatureBinding } from '../feature-registry';
import { t } from '../i18n';
import { createSlidesEditor } from './slides/controller';
import type { SlidesApi } from './slides/contracts';

const editor = createSlidesEditor({
  document,
  translate: t,
  getApi: () => (getFeatureBinding('Api') as { slides?: SlidesApi } | undefined)?.slides ?? null,
  notifyApplied: () => {
    const show = getFeatureBinding('showToast');
    if (typeof show === 'function') {
      try { show('', t('sled.applied'), '', 2400); } catch { /* Cosmetic toast failure cannot prevent receipt reconciliation. */ }
    }
  },
  prefill: message => {
    const fill = getFeatureBinding('TofuComposerPrefill');
    if (typeof fill === 'function') fill(message, false);
  },
});
Object.assign(featureRegistry, { openSlidesEditor: editor.open, closeSlidesEditor: editor.close });

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('slides-editor', name, args, stub);
}
