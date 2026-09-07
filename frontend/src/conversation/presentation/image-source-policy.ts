/**
 * Pure allowlist policy for untrusted image and external-asset sources.
 *
 * Responsibility: normalize only explicitly supported URL forms for typed
 * presentation owners. Entry points: `safeImageSource` and
 * `safeExternalAssetUrl`. This module has no DOM, browser-global, decoding,
 * fetching, or rendering dependency.
 */

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function isSafeRelativeUrl(value: string): boolean {
  return /^(?:\/(?!\/)|\.\.?\/)/.test(value);
}

export function safeImageSource(value: unknown): string {
  const source = stringValue(value).trim();
  if (!source) return '';
  if (/^https?:\/\//i.test(source)) return source;
  if (/^blob:(?:https?:\/\/|null\/)/i.test(source)) return source;
  if (isSafeRelativeUrl(source)) return source;
  return /^data:image\/(?:avif|gif|jpe?g|png|svg\+xml|webp);base64,[a-z0-9+/]*={0,2}$/i
      .test(source)
    ? source
    : '';
}

export function safeExternalAssetUrl(value: unknown): string {
  const url = stringValue(value).trim();
  if (/^https?:\/\//i.test(url) || isSafeRelativeUrl(url)) return url;
  return '';
}
