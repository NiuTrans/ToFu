/**
 * Responsibility: own the DOM state transitions for accepting/dismissing a
 * learned preference and undoing a My Context change.
 * Entry point: createPreferenceActionsController. Dependencies: injected
 * mutation, translation, icon, and failure-reporting ports only.
 */

export type PreferenceActionTranslationKey =
  | 'prefs.learnedReinforced'
  | 'prefs.dismiss'
  | 'context.undoing'
  | 'context.undone';

export interface PreferenceActionsDependencies {
  resolvePendingPreference(pendingId: string, accept: boolean): Promise<void>;
  undoContextChange(changeId: string): Promise<void>;
  translate(key: PreferenceActionTranslationKey): string;
  iconHtml(name: 'check' | 'x', size: number): string;
  reportResolveFailure?(error: unknown): void;
  reportUndoFailure?(error: unknown): void;
}

export interface PreferenceActionsController {
  resolvePreference(
    button: Element | null,
    pendingId: string,
    accept: boolean,
  ): Promise<void>;
  undoContextChange(
    button: HTMLButtonElement | null,
    changeId: string,
  ): Promise<void>;
}

function reportFailure(
  reporter: ((error: unknown) => void) | undefined,
  error: unknown,
): void {
  try {
    reporter?.(error);
  } catch {
    // A presentation/logging failure must not undo the UI rollback above.
  }
}

export function createPreferenceActionsController(
  dependencies: PreferenceActionsDependencies,
): PreferenceActionsController {
  const resolvePreference = async (
    button: Element | null,
    pendingId: string,
    accept: boolean,
  ): Promise<void> => {
    const row = button?.closest('.pl-row') as HTMLElement | null;
    if (row) {
      row.style.opacity = '0.5';
      row.style.pointerEvents = 'none';
    }
    try {
      await dependencies.resolvePendingPreference(pendingId, accept);
      if (!row) return;
      row.innerHTML = '<span class="pl-lead">'
        + dependencies.iconHtml(accept ? 'check' : 'x', 13)
        + '</span><span class="pl-text">'
        + dependencies.translate(
          accept ? 'prefs.learnedReinforced' : 'prefs.dismiss',
        )
        + '</span>';
      row.classList.add('pl-resolved');
      row.style.opacity = '';
    } catch (error) {
      if (row) {
        row.style.opacity = '';
        row.style.pointerEvents = '';
      }
      reportFailure(dependencies.reportResolveFailure, error);
    }
  };

  const undoContextChange = async (
    button: HTMLButtonElement | null,
    changeId: string,
  ): Promise<void> => {
    if (!changeId || !button) return;
    const previousText = button.textContent;
    button.disabled = true;
    button.textContent = dependencies.translate('context.undoing');
    try {
      await dependencies.undoContextChange(changeId);
      button.textContent = dependencies.translate('context.undone');
      button.classList.add('is-undone');
    } catch (error) {
      button.disabled = false;
      button.textContent = previousText;
      reportFailure(dependencies.reportUndoFailure, error);
    }
  };

  return Object.freeze({ resolvePreference, undoContextChange });
}
