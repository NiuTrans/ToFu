/**
 * Injected localized copy, placeholder fallback, duration and diagnostic text
 * formatting. No DOM, transport or catalog.
 * Entry points: textFor, formatDuration, displayJson.
 * Dependencies: ports.
 */
import type { ClassicConversationRendererPorts } from './ports';

export function textFor(
  ports: Pick<ClassicConversationRendererPorts, 'localizedText'>,
  key: string,
  fallback: string,
  values?: Readonly<Record<string, string | number>>,
): string {
  const localized = ports.localizedText?.(key, fallback, values);
  if (localized != null) return localized;
  if (!values) return fallback;
  /* A port-less fallback must still be renderable — substitute the same
   * {placeholder} vocabulary translateMessage applies. */
  return fallback.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name] ?? '')
      : token
  ));
}

export function formatDuration(seconds: number | undefined): string {
  const rounded = Math.max(0, Math.round(seconds ?? 0));
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

export function displayJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value ?? '');
  }
}
