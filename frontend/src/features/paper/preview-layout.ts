/** Device-local preview layout preferences; contains no project or owner data.
 * Only deliberate adjustments persist. Old auto-saved defaults migrate to the
 * paper-first layout while nondefault user adjustments retain their width.
 */
import { readBrowserStorage, removeBrowserStorage, writeBrowserStorage } from '../../core/browser-storage';

export const PREVIEW_CHAT_PERCENT = { initial: 40, minimum: 30, maximum: 70 } as const;
const KEY = 'tofu_paper_preview_layout';
const LEGACY_KEY = 'tofu_paper_preview_split';

export function clampPreviewSplit(value: number): number {
  return Number.isFinite(value) ? Math.max(PREVIEW_CHAT_PERCENT.minimum,
    Math.min(PREVIEW_CHAT_PERCENT.maximum, value)) : PREVIEW_CHAT_PERCENT.initial;
}

export function readPreviewSplit(): number {
  const raw = readBrowserStorage(KEY);
  if (raw && raw.length <= 128) {
    try {
      const value: unknown = JSON.parse(raw);
      if (value && typeof value === 'object' && 'chatPercent' in value
        && typeof value.chatPercent === 'number' && Number.isFinite(value.chatPercent)) {
        return clampPreviewSplit(value.chatPercent);
      }
    } catch { /* Browser preferences cannot prevent opening the paper. */ }
  }
  const legacy = readBrowserStorage(LEGACY_KEY);
  const value = legacy && legacy.length <= 32 ? Number(legacy) : NaN;
  // The old constructor persisted 55 even when the divider was never moved.
  const restored = Number.isFinite(value) && value >= 35 && value <= 70 && value !== 55
    ? value : PREVIEW_CHAT_PERCENT.initial;
  if (legacy !== null) {
    if (persistPreviewSplit(restored)) removeBrowserStorage(LEGACY_KEY);
  }
  return restored;
}

export function persistPreviewSplit(value: number): boolean {
  return writeBrowserStorage(KEY, JSON.stringify({ chatPercent: clampPreviewSplit(value) }));
}
