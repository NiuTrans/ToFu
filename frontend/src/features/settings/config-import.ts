/** File import coordination. Shares Save's command latch and view feedback.
 * Entry: importServerConfig. A retired file read sends no command; an accepted
 * update settles independently and refreshes only its unchanged original view.
 */
import {featureRegistry} from '../../feature-registry';
import {t} from '../../i18n';
import {beginSettingsSaveFeedback} from './save-feedback';

interface ImportPorts {
  Api?: {serverConfig?: {update(payload: unknown): Promise<{json(): Promise<{ok?: boolean; error?: string}>} | null>}};
  debugLog?: (message: string, level?: string) => void;
  _serverConfig?: unknown;
  _refreshServerConfigAfterSave?: () => Promise<void>;
  openSettings?: () => void;
}

function readImportFile(file: File, signal?: AbortSignal): Promise<string | null> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { resolve(null); return; }
    const reader = new FileReader();
    const clear = () => {
      signal?.removeEventListener('abort', abort);
      reader.onload = reader.onerror = reader.onabort = null;
    };
    const abort = () => { clear(); reader.abort(); resolve(null); };
    reader.onload = event => { clear(); resolve(String(event.target?.result || '')); };
    reader.onerror = () => { clear(); reject(reader.error || new Error('Cannot read configuration file')); };
    reader.onabort = () => { clear(); resolve(null); };
    signal?.addEventListener('abort', abort, {once: true});
    try { reader.readAsText(file); }
    catch (error) { clear(); reject(error); }
  });
}

export async function importServerConfig(event: Event): Promise<void> {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  target.value = '';
  const feedback = beginSettingsSaveFeedback();
  if (!feedback) return;
  const bridge = featureRegistry as unknown as ImportPorts;
  try {
    feedback.watchSubmittedForm();
    const text = await readImportFile(file, feedback.session?.scope.signal);
    if (text === null || !feedback.isCurrent()) return;
    const imported: unknown = JSON.parse(text);
    const response = await bridge.Api?.serverConfig?.update(imported);
    const data = response ? await response.json() : null;
    if (!data?.ok) throw new Error(data?.error || 'server-config import returned no acknowledgement');
    bridge.debugLog?.('[Settings] Config imported successfully', 'success');
    // Application policy follows the committed result even if its editor closed.
    void bridge._refreshServerConfigAfterSave?.().catch(error => {
      bridge.debugLog?.('[Settings] Imported config refresh failed: ' + String(error), 'error');
    });
    if (!feedback.isCurrent()) return;
    if (feedback.wasEdited()) {
      feedback.show(`${t('settings.saved')} · ${t('context.unsaved')}`);
      return;
    }
    bridge._serverConfig = null;
    bridge.openSettings?.();
  } catch (error) {
    bridge.debugLog?.('[Settings] Import failed: ' + String(error), 'error');
    feedback.show(t('context.saveFailed') + ': ' + String(error));
  } finally { feedback.finish(); }
}
