/** Backend resource metadata stays primary. Historical LLM chips recover exact
 * call-local IDs through the shared source-verified route contract, never names
 * or error text. Shared by retained and typed renderers; no network/state writes.
 */
import { escapeHtmlText } from '../../html-safety';
import { recoverLlmCanvasLinks } from './llm-canvas-links';

export function renderMcpResourceLinks(escapedText: string, value: unknown): string {
  const row = value && typeof value === 'object' ? value as Record<string, unknown> : {};

  if (escapedText.length > 16_384) return escapedText;
  const recovered = recoverLlmCanvasLinks(row);
  if (recovered.length) {
    const chips = recovered.map(link => `<a class="ptool-mcp-link" data-mcp-canvas-role="${escapeHtmlText(link.role)}" href="${escapeHtmlText(link.href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtmlText(link.href)}">${escapeHtmlText(link.label)}</a>`)
      .filter(anchor => !escapedText.includes(anchor));
    return escapedText + (chips.length ? ` <span class="ptool-mcp-resources">· ${chips.join(' · ')}</span>` : '');
  }
  const links = row._mcpLinks;
  if (!links || typeof links !== 'object' || Array.isArray(links) || escapedText.length > 16_384) return escapedText;
  const candidates: { label: string; href: string }[] = [];
  let inspected = 0;
  for (const label in links) {
    if (!Object.hasOwn(links, label)) continue;
    if (++inspected > 32) break;
    const href = (links as Record<string, unknown>)[label];
    if (!label || label.length > 256 || typeof href !== 'string' || href.length > 2048
        || /[\u0000-\u0020\u007f]/.test(href)) continue;
    try {
      const url = new URL(href);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) continue;
    } catch { continue; }
    candidates.push({label: escapeHtmlText(label), href: escapeHtmlText(href)});
  }
  candidates.sort((a,b)=>b.label.length-a.label.length);
  const used = new Set<string>();
  let inAnchor = false;
  return escapedText.split(/(<[^>]*>)/g).map(part => {
    if (part.startsWith('<')) {
      if (/^<a(?:\s|>)/i.test(part)) inAnchor = true;
      if (/^<\/a\s*>/i.test(part)) inAnchor = false;
      return part;
    }
    if (inAnchor) {
      for (const candidate of candidates) if (part.includes(candidate.label)) used.add(candidate.label);
      return part;
    }
    let tail = part;
    let output = '';
    while (tail) {
      let match: typeof candidates[number] | undefined;
      let position = tail.length;
      for (const candidate of candidates) {
        if (used.has(candidate.label)) continue;
        const found = tail.indexOf(candidate.label);
        if (found >= 0 && found < position) { position = found; match = candidate; }
      }
      if (!match) return output + tail;
      output += tail.slice(0, position) + `<a class="ptool-mcp-link" href="${match.href}" target="_blank" rel="noopener noreferrer" title="${match.href}">${match.label}</a>`;
      tail = tail.slice(position + match.label.length);
      used.add(match.label);
    }
    return output;
  }).join('');
}
