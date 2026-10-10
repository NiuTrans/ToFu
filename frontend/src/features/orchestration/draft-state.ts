import { orchestrationRegistry } from './registry';
import { createOrchestrationBoundedState } from './bounded-state';

interface DraftInput extends HTMLElement {
  value: string;
  selectionStart?: number | null;
  selectionEnd?: number | null;
  selectionDirection?: 'forward' | 'backward' | 'none' | null;
  focus(options?: FocusOptions): void;
  setSelectionRange?(
    start: number,
    end: number,
    direction?: 'forward' | 'backward' | 'none',
  ): void;
}

export interface DraftStateOptions {
  maxEntries?: unknown;
}

interface DraftSelection {
  start: number;
  end: number;
  direction: 'forward' | 'backward' | 'none';
}

export interface OrchestrationDraftState {
  bind(element: DraftInput | null, key: unknown): () => void;
  capture(element: DraftInput | null, key: unknown): string;
  read(key: unknown, fallback?: unknown): string;
  write(key: unknown, value: unknown): string;
  clear(key: unknown): void;
  clearAll(): void;
}

type DraftStateWindow = Window & {
  createOrchestrationDraftState?: typeof createOrchestrationDraftState;
};

/** Bounded unsent input state with focus and selection restoration. */
export function createOrchestrationDraftState(
  options: DraftStateOptions = {},
): OrchestrationDraftState {
  let selections: Record<string, DraftSelection> = Object.create(null);
  let focusedKey = '';
  const values = createOrchestrationBoundedState<string>({
    maxEntries: options.maxEntries,
    fallbackMaxEntries: 128,
    onRemove: (key) => {
      delete selections[key];
      if (focusedKey === key) focusedKey = '';
    },
  });
  const write = (key: unknown, value: unknown): string => {
    return values.set(key, String(value == null ? '' : value));
  };
  const read = (key: unknown, fallback?: unknown): string => {
    const normalized = values.key(key);
    if (!values.has(normalized)) {
      return String(fallback == null ? '' : fallback);
    }
    return write(normalized, values.get(normalized));
  };
  const capture = (element: DraftInput | null, key: unknown): string =>
    element && element.value != null
      ? write(key, element.value) : read(key, '');
  const captureSelection = (element: DraftInput, key: string): void => {
    const owner = element.ownerDocument;
    if (!owner || owner.activeElement !== element) return;
    focusedKey = key;
    selections[key] = {
      start: Number(element.selectionStart),
      end: Number(element.selectionEnd),
      direction: element.selectionDirection || 'none',
    };
  };
  const restoreSelection = (element: DraftInput, key: string): void => {
    if (focusedKey !== key) return;
    const selection = selections[key];
    try {
      element.focus({ preventScroll: true });
    } catch {
      element.focus();
    }
    if (typeof element.setSelectionRange === 'function'
        && Number.isFinite(selection?.start)
        && Number.isFinite(selection?.end)) {
      const length = String(element.value || '').length;
      try {
        element.setSelectionRange(
          Math.min(selection?.start ?? 0, length),
          Math.min(selection?.end ?? 0, length),
          selection?.direction,
        );
      } catch {
        // Unsupported input types retain the draft without selection state.
      }
    }
    focusedKey = '';
    delete selections[key];
  };
  const bind = (element: DraftInput | null, key: unknown): (() => void) => {
    if (!element) return () => {};
    const normalized = values.key(key);
    if (values.has(normalized)) {
      element.value = read(normalized, '');
    }
    restoreSelection(element, normalized);
    const onInput = (): void => { capture(element, normalized); };
    element.addEventListener('input', onInput);
    return () => {
      captureSelection(element, normalized);
      capture(element, normalized);
      element.removeEventListener('input', onInput);
    };
  };
  const clear = (key: unknown): void => { values.remove(key); };
  const clearAll = (): void => {
    values.clear();
    selections = Object.create(null);
    focusedKey = '';
  };

  return { bind, capture, read, write, clear, clearAll };
}

(orchestrationRegistry as unknown as DraftStateWindow).createOrchestrationDraftState =
  createOrchestrationDraftState;
