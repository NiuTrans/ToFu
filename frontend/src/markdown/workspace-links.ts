/** Workspace links are presentation hints, never authority to fetch/publish files.
 * Entry points: isWorkspaceFileHref, explainWorkspaceLinks.
 * Dependencies: DOM only. Run on an inert template BEFORE URI sanitization so
 * file: sources survive as text; the normal sanitizer still admits the result.
 */
import { escapeHtml } from '../html-safety';

import { safeArtifactDownloadHref } from './safe-delivery-links';
const FILE_EXTENSION = /\.(?:md|markdown|html?|svg|pdf|txt|csv|tsv|json|ya?ml|xml|zip|tar|gz|bin|docx?|xlsx?|pptx?|py|[cm]?js|tsx?|css|png|jpe?g|webp|gif)(?:$|[?#])/i;
const OS_ROOT = /^\/(?:mnt|home|Users|Volumes|tmp|var|private|etc|usr|opt|dev|proc|sys|run|media|data|workspace|workspaces|srv|root)(?:\/|$)/i;

/** Preserve ambiguous web-relative URLs; only clear file intent is inert. */
export function isWorkspaceFileHref(href: string, basePath = ''): boolean {
  const source = href.trim();
  if (!source || /^(?:#|mailto:|https?:\/\/)/i.test(source)) return false;
  if (/^(?:file:|filesystem:|vscode:\/\/file\/)/i.test(source)) return true;
  if (/^[a-z][a-z0-9+.-]*:/i.test(source) && !/^[a-z]:[\\/]/i.test(source)) return false;
  let path = source;
  // Encoded path markers do not become a navigation escape. Never resolve them.
  for (let round = 0; round < 2; round++) {
    try { const decoded = decodeURIComponent(path); if (decoded === path) break; path = decoded; }
    catch { break; }
  }
  if (/^(?:file:|filesystem:|vscode:\/\/file\/)/i.test(path)) return true;
  if (/^[a-z]:[\\/]|^\\\\|^~[\\/]/i.test(path)) return true;
  if (path.startsWith('//')) return false; // protocol-relative web URL
  if (basePath && (path === basePath || path.startsWith(basePath + '/'))) return false;
  if (OS_ROOT.test(path)) return true;

  if (/^\/\.worktrees\//i.test(path) || (/^\/designs\//i.test(path) && FILE_EXTENSION.test(path))) return true;
  if (path.startsWith('/')) return false; // app routes, including /docs/*.md
  if (/^\.{1,2}[\\/]/.test(path)) return FILE_EXTENSION.test(path) || /(?:^|\/)\.worktrees\//i.test(path);
  if (path.includes('\\')) return true;
  if (/^\.worktrees[\/]/i.test(path)) return true;
  if (/^(?:designs|docs)[\/]/i.test(path) && FILE_EXTENSION.test(path)) return true;
  // Bare web filenames (guide.html/report.pdf/image.png) are ambiguous.
  // Only canonical repository guidance/readme basenames are clear workspace intent.
  return /^(?:README|AGENTS|CLAUDE|JOURNAL|CHANGELOG|CONTRIBUTING|LICENSE)(?:\.(?:md|markdown|txt))?(?:$|[?#])/i.test(path);
}

export function explainWorkspaceLinks(html: string, document: Document | null, basePath: string,
  translate?: (key: string, fallback: string) => string): string {
  if (!document) return escapeHtml(html); // no HTML parser: source-only, never navigable

  // Text/code-only responses must not allocate/parse a scratch DOM tree.
  if (!/<(?:a|area)\b/i.test(html)) return html;
  const text = (key: string, fallback: string) => translate?.(key, fallback) ?? fallback;
  const template = document.createElement('template');
  template.innerHTML = html;
  let changed = false;
  for (const anchor of template.content.querySelectorAll('a[href], area[href], a[xlink\\:href]')) {
    const attribute = anchor.hasAttribute('href') ? 'href' : 'xlink:href';
    const href = anchor.getAttribute(attribute) ?? '';
    if (!isWorkspaceFileHref(href, basePath)) {
      const safe = safeArtifactDownloadHref(href, document.location?.origin ?? '');
      if (safe !== href) { anchor.setAttribute(attribute, safe); changed = true; }
      continue;
    }
    const note = document.createElement('span');
    note.className = 'md-inaccessible-attachment';
    note.setAttribute('role', 'note');
    anchor.querySelectorAll('script,style').forEach(node => node.remove());
    const label = anchor.textContent?.trim() || anchor.getAttribute('alt') || anchor.querySelector('img')?.getAttribute('alt') || href;
    note.append(document.createTextNode(label + ' — ' + text('delivery.unpublished', 'Unpublished workspace file') + ': '));
    const source = document.createElement('code');
    source.textContent = href;
    note.append(source, document.createTextNode('. ' + text('delivery.publishHelp', 'Publish or upload this file to open or download it here.')));
    anchor.replaceWith(note);
    changed = true;
  }
  return changed ? template.innerHTML : html;
}
