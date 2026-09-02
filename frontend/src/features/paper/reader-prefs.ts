import { registerAction } from '../../action-registry';
import { t, type I18nKey } from '../../i18n';

export interface ReaderPreferences {
  scaleIdx: number;
  widthIdx: number;
}

const PREFS_KEY = 'paper_reader_prefs';
const FONT_SCALES = [0.85, 0.925, 1.0, 1.1, 1.2, 1.3] as const;
const DEFAULT_SCALE_INDEX = 2;
const WIDTHS = [
  { measure: '60ch', label: 'paper.readerWidthNarrow' },
  { measure: '68ch', label: 'paper.readerWidthComfortable' },
  { measure: '78ch', label: 'paper.readerWidthWide' },
] as const satisfies readonly { measure: string; label: I18nKey }[];
const DEFAULT_WIDTH_INDEX = 1;

function clampIndex(value: unknown, maximum: number, fallback: number): number {
  const numeric = typeof value === 'number' ? value : fallback;
  return Math.max(0, Math.min(maximum, numeric | 0));
}

/** Read global reader comfort preferences. Corrupt storage fails closed. */
export function readReaderPreferences(): ReaderPreferences {
  let scaleIdx: unknown = DEFAULT_SCALE_INDEX;
  let widthIdx: unknown = DEFAULT_WIDTH_INDEX;
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        const record = parsed as Record<string, unknown>;
        scaleIdx = record.scaleIdx;
        widthIdx = record.widthIdx;
      }
    }
  } catch (error: unknown) {
    console.warn('[Paper:Reader] read prefs failed:', error);
  }
  return {
    scaleIdx: clampIndex(
      scaleIdx, FONT_SCALES.length - 1, DEFAULT_SCALE_INDEX),
    widthIdx: clampIndex(widthIdx, WIDTHS.length - 1, DEFAULT_WIDTH_INDEX),
  };
}

/** Persist one validated snapshot. Storage denial never breaks the reader. */
export function persistReaderPreferences(prefs: ReaderPreferences): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      scaleIdx: prefs.scaleIdx,
      widthIdx: prefs.widthIdx,
    }));
  } catch (error: unknown) {
    console.warn('[Paper:Reader] persist prefs failed:', error);
  }
}

/** Apply preferences to both report and review surfaces and sync controls. */
export function applyReaderPreferences(
  prefs: ReaderPreferences = readReaderPreferences(),
): ReaderPreferences {
  const normalized = {
    scaleIdx: clampIndex(
      prefs.scaleIdx, FONT_SCALES.length - 1, DEFAULT_SCALE_INDEX),
    widthIdx: clampIndex(
      prefs.widthIdx, WIDTHS.length - 1, DEFAULT_WIDTH_INDEX),
  };
  const scale = FONT_SCALES[normalized.scaleIdx];
  const width = WIDTHS[normalized.widthIdx];

  for (const id of ['paperReportContent', 'paperReviewContent']) {
    const element = document.getElementById(id);
    if (!element) continue;
    element.style.setProperty('--reader-font-scale', String(scale));
    element.style.setProperty('--reader-measure', width.measure);
  }

  const labelText = t(width.label);
  document.querySelectorAll<HTMLElement>('.paper-reader-width-label')
    .forEach((label) => { label.textContent = labelText; });
  document.querySelectorAll<HTMLButtonElement>('.paper-reader-set-dec')
    .forEach((button) => { button.disabled = normalized.scaleIdx <= 0; });
  document.querySelectorAll<HTMLButtonElement>('.paper-reader-set-inc')
    .forEach((button) => {
      button.disabled = normalized.scaleIdx >= FONT_SCALES.length - 1;
    });
  return normalized;
}

/** Nudge reading text size by one discrete step. */
export function readerFontStep(direction: number): void {
  const prefs = readReaderPreferences();
  const next = Math.max(0, Math.min(
    FONT_SCALES.length - 1,
    prefs.scaleIdx + (direction > 0 ? 1 : -1),
  ));
  if (next === prefs.scaleIdx) return;
  prefs.scaleIdx = next;
  persistReaderPreferences(prefs);
  applyReaderPreferences(prefs);
}

/** Cycle Narrow → Comfortable → Wide → Narrow. */
export function readerWidthCycle(): void {
  const prefs = readReaderPreferences();
  prefs.widthIdx = (prefs.widthIdx + 1) % WIDTHS.length;
  persistReaderPreferences(prefs);
  applyReaderPreferences(prefs);
}

registerAction('_readerFontStep', readerFontStep as (...args: unknown[]) => unknown);
registerAction('_readerWidthCycle', readerWidthCycle as (...args: unknown[]) => unknown);
