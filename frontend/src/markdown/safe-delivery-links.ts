/** Untrusted links may request safe downloads, never select executable mode.
 * Entry: safeArtifactDownloadHref. Pure URL inspection, no fetch/resolution of
 * workspace paths. Trusted native controls use the endpoint facade directly.
 */
function decoded(value: string): string {
  for (let round = 0; round < 3; round++) {
    try { const next = decodeURIComponent(value); if (next === value) break; value = next; }
    catch { break; }
  }
  return value;
}

export function safeArtifactDownloadHref(href: string, origin: string): string {
  const source = decoded(href.trim());
  const absolute = /^(?:https?:|\/\/)/i.test(source);
  // A sentinel allows relative URLs in detached test documents without granting
  // their HTTP origins same-origin status. The application supplies real origin.
  const base = origin && origin !== 'null' ? origin : 'https://inert.invalid';
  let url: URL;
  try { url = new URL(source, base + '/'); } catch { return href; }
  if (!/^https?:$/.test(url.protocol) || url.origin !== base) return href;
  const panelDownload = /(?:^|\/)api\/artifacts\/[^/]+\/download$/.test(url.pathname);
  const ownedResolver = /(?:^|\/)api\/response-assets\/artifacts\/[^/]+$/.test(url.pathname);
  if (!panelDownload && !ownedResolver) return href;
  // Preserve resolver conversation/download context while removing every mode
  // selector, including nested percent encoding and duplicate keys.
  const selectors = [...url.searchParams.keys()].filter(key => decoded(key).toLowerCase() === 'interactive');
  if (!selectors.length) return href;
  for (const key of selectors) url.searchParams.delete(key);
  return (absolute ? url.origin : '') + url.pathname + url.search + url.hash;
}
