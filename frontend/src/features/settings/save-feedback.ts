/** One global save latch, with feedback owned by its original Settings view.
 * Closing retires presentation only; accepted writes retain the latch until
 * settlement. Entry points: beginSettingsSaveFeedback, retireSettingsSaveFeedback.
 * Depends on the shared Settings read session and browser lifecycle scope.
 */
import {t} from '../../i18n';
import {createLifecycleScope} from '../../lifecycle';
import {currentSettingsReadSession} from './panel-read-session';

export interface SettingsSaveFeedback {
  readonly session: ReturnType<typeof currentSettingsReadSession>;
  isCurrent(): boolean;
  wasEdited(): boolean;
  watchSubmittedForm(): void;
  show(message: string): void;
  retire(): void;
  finish(): void;
}

let active: SettingsSaveFeedback | null = null;

function setSaveButtonBusy(busy: boolean): void {
  const button = document.getElementById('settingsSaveBtn');
  if (button instanceof HTMLButtonElement) button.disabled = busy;
}

export function retireSettingsSaveFeedback(): void { active?.retire(); }

export function beginSettingsSaveFeedback(): SettingsSaveFeedback | null {
  if (active) return null;
  const session = currentSettingsReadSession();
  const modal = document.getElementById('settingsModal');
  const hint = document.getElementById('settingsStatusHint');
  const scope = typeof HTMLElement !== 'undefined' && modal instanceof HTMLElement
    ? createLifecycleScope(window) : null;
  let retired = false, edited = false, watching = false;
  const feedback: SettingsSaveFeedback = {
    session,
    isCurrent: () => !retired && currentSettingsReadSession() === session
      && (!session || session.isCurrent()) && document.getElementById('settingsModal') === modal,
    wasEdited: () => edited,
    watchSubmittedForm() {
      if (watching || !scope || !modal || !feedback.isCurrent()) return;
      watching = true;
      scope.listen(modal, 'input', () => { edited = true; });
      scope.listen(modal, 'change', () => { edited = true; });
    },
    show(message) {
      if (feedback.isCurrent() && hint && document.getElementById('settingsStatusHint') === hint) hint.textContent = message;
    },
    retire() { retired = true; scope?.destroy(); },
    finish() {
      if (active !== feedback) return;
      active = null;
      setSaveButtonBusy(false);
      if (hint?.textContent === t('common.saving')) feedback.show('');
      scope?.destroy();
    },
  };
  if (scope && session) scope.listen(session.scope.signal, 'abort', () => feedback.retire(), {once: true});
  active = feedback;
  setSaveButtonBusy(true);
  feedback.show(t('common.saving'));
  return feedback;
}
