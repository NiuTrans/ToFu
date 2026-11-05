/** Local Knowledge Settings tab lifecycle. Corpus state stays in the
 * demand-loaded knowledge presenter; this owner only coordinates visibility. */
import {featureRegistry} from '../../feature-registry';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';
import {t} from '../../i18n';
import {jumpToSetting} from './setting-jump';
import type {FeatureCallable} from '../../runtime-bridge';

let generation = 0;
let loaded = false;

function active(session: SettingsReadSession | null): boolean {
  return !!session?.isCurrent()
    && document.getElementById('settingsTab_knowledge')?.classList.contains('active') === true;
}

function close(): void {
  if (!loaded) return;
  const closeKnowledge = featureRegistry.closeKnowledgeBase as FeatureCallable | undefined;
  if (typeof closeKnowledge === 'function') closeKnowledge();
}

export function deactivateKnowledgeSettingsTab(tabId: string): void {
  if (tabId === 'knowledge') return;
  generation++;
  close();
}

export function openKnowledgeSettings(): void {
  jumpToSetting('knowledge');
}

export async function populateKnowledgeSettingsTab(): Promise<void> {
  const session = currentSettingsReadSession();
  const request = ++generation;
  try {
    await import('../../runtime/knowledge-presenters.generated.js');
    loaded = true;
    if (request !== generation || !active(session)) { close(); return; }
    const openKnowledge = featureRegistry.openKnowledgeBase as FeatureCallable | undefined;
    if (typeof openKnowledge === 'function') openKnowledge();
  } catch (error) {
    if (request !== generation || !active(session)) return;
    const root = document.getElementById('knowledgeDocs');
    root?.replaceChildren();
    const message = document.createElement('p');
    message.className = 'settings-field-hint';
    message.textContent = error instanceof Error ? error.message : String(error);
    root?.append(message);
    (featureRegistry.showToast as FeatureCallable | undefined)?.(t('feature.loadFailed'), 'error');
  }
}

export function destroyKnowledgeSettingsTab(): void {
  generation++;
  close();
}

featureRegistry._deactivateKnowledgeSettingsTab = deactivateKnowledgeSettingsTab;
featureRegistry._populateKnowledgeTab = populateKnowledgeSettingsTab;
featureRegistry._destroyKnowledgeTab = destroyKnowledgeSettingsTab;
