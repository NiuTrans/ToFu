/** Shared artifact presentation primitives; no state, timers or registration. */
import { featureRegistry } from '../../feature-registry';

export function formatMediaTime(value: number): string {
  const seconds = Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
export function mediaNode<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = ''): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag); el.className = className; el.textContent = text; return el;
}
export function mediaArtifactUrl(value: string): string {
  try {
    const apiUrl = featureRegistry.apiUrl as ((url: string) => string) | undefined;
    const url = new URL(apiUrl?.(value) ?? value, window.location.href);
    return /^https?:$/.test(url.protocol) ? url.href : '';
  } catch { return ''; }
}
export function downloadMediaArtifact(url: string, name: string): void {
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
}
