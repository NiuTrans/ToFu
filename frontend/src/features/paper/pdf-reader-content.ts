/** Complete PDF content features. No retained document, text index or DOM cache. */
import type { PdfDocument, PdfPage } from './pdf-reader';

export interface OutlineItem { title: string; destination: string | unknown[]; depth: number }
export interface SearchMatch { page: number; excerpt: string }
export interface SearchResult { matches: SearchMatch[]; truncated: boolean }

/** Horizontal text extent on the requested half of a page. This is a zoom
 * target, not inferred reading order or PDF reflow. Scans complete page text.
 */
export async function columnTextBounds(page: PdfPage, side: 'left' | 'right'): Promise<{ left: number; right: number }> {
  const viewport = page.getViewport({ scale: 1 });
  const half = viewport.width / 2;
  const fallback = side === 'left' ? { left: 0, right: half } : { left: half, right: viewport.width };
  const content = await page.getTextContent();
  let left = Infinity, right = -Infinity, count = 0;
  for (const item of content.items) {
    if (!('str' in item)) continue;
    if (!item.str.trim() || Math.abs(item.transform[1]) > 0.01 || Math.abs(item.transform[2]) > 0.01) continue;
    const start = viewport.convertToViewportPoint(item.transform[4], item.transform[5]);
    const end = viewport.convertToViewportPoint(item.transform[4] + item.width, item.transform[5]);
    // A centered footer page number must not widen either body column.
    if (/^\d+$/.test(item.str.trim()) && (start[1] > viewport.height * 0.9 || start[1] < viewport.height * 0.05)) continue;
    const x1 = Math.min(start[0], end[0]), x2 = Math.max(start[0], end[0]);
    if (!Number.isFinite(x1 + x2) || x2 <= x1 || x1 < 0 || x2 > viewport.width) continue;
    if (side === 'left' ? x2 > half : x1 < half) continue;
    left = Math.min(left, x1); right = Math.max(right, x2); count++;
  }
  if (count < 3 || right - left < viewport.width * 0.15) return fallback;
  return { left: Math.max(0, left - 8), right: Math.min(viewport.width, right + 8) };
}

export async function readOutline(document: PdfDocument): Promise<OutlineItem[]> {
  const outline = await document.getOutline();
  const result: OutlineItem[] = [];
  const queue = [...(outline ?? [])]
    .reverse().map((item) => ({ item, depth: 0 }));
  const seen = new WeakSet<object>();
  while (queue.length) {
    const { item, depth } = queue.pop()!;
    if (seen.has(item)) continue;
    seen.add(item);
    if (typeof item.dest === 'string' || Array.isArray(item.dest)) {
      result.push({ title: String(item.title ?? ''), destination: item.dest, depth });
    }
    for (const child of [...(item.items ?? [])].reverse()) {
      queue.push({ item: child, depth: depth + 1 });
    }
  }
  return result;
}

export async function searchDocument(
  document: PdfDocument, query: string, cancelled: () => boolean,
): Promise<SearchResult> {
  const needle = query.trim().normalize('NFKC').replace(/\s+/gu, ' ').toLocaleLowerCase();
  const matches: SearchMatch[] = [];
  if (!needle) return { matches, truncated: false };
  for (let number = 1; number <= document.numPages; number++) {
    if (cancelled()) return { matches: [], truncated: false };
    const page = await document.getPage(number);
    const content = await page.getTextContent();
    if (cancelled()) return { matches: [], truncated: false };
    const parts: string[] = [];
    for (const item of content.items) {
      if (!('str' in item)) continue;
      if (cancelled()) return { matches: [], truncated: false };
      parts.push(item.str);
      if (parts.length % 1024 === 0) await new Promise<void>((resolve) => setTimeout(resolve, 0));
    }
    const text = parts.join(' ').normalize('NFKC').replace(/\s+/gu, ' ');
    const folded = text.toLocaleLowerCase();
    let offset = 0;
    while ((offset = folded.indexOf(needle, offset)) >= 0) {
      matches.push({ page: number, excerpt: text.slice(Math.max(0, offset - 36), offset + needle.length + 72) });
      if (cancelled()) return { matches: [], truncated: false };
      offset += needle.length;
      if (matches.length % 1024 === 0) await new Promise<void>((resolve) => setTimeout(resolve, 0));
    }
    // Yield to scrolling/cancellation. PDF.js owns page objects; do not clean
    // a page here while another consumer may be rendering its operator list.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
  return { matches, truncated: false };
}

export async function addPageLinks(
  page: PdfPage, viewport: ReturnType<PdfPage['getViewport']>, container: HTMLElement,
  navigate: (destination: string | unknown[]) => void,
): Promise<void> {
  const annotations = await page.getAnnotations({ intent: 'display' });
  const layer = document.createElement('div');
  layer.className = 'paper-reader-links';
  for (const annotation of annotations) {
    if (annotation.subtype !== 'Link' || !Array.isArray(annotation.rect)
      || annotation.rect.length !== 4 || !annotation.rect.every(Number.isFinite)) continue;
    const link = document.createElement('a');
    const destination: unknown = annotation.dest;
    if (typeof annotation.url === 'string') {
      try {
        const url = new URL(annotation.url);
        if (!['https:', 'http:', 'mailto:'].includes(url.protocol)) continue;
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.setAttribute('aria-label', url.href.slice(0, 512));
      } catch { continue; }
    } else if (typeof destination === 'string' || Array.isArray(destination)) {
      link.href = '#';
      link.setAttribute('aria-label', String(annotation.titleObj?.str || annotation.contentsObj?.str || '↗').slice(0, 512));
      link.addEventListener('click', (event) => { event.preventDefault(); navigate(destination); });
    } else continue;
    const rect = viewport.convertToViewportRectangle(annotation.rect);
    const left = Math.max(0, Math.min(rect[0], rect[2]));
    const top = Math.max(0, Math.min(rect[1], rect[3]));
    const width = Math.min(viewport.width - left, Math.abs(rect[2] - rect[0]));
    const height = Math.min(viewport.height - top, Math.abs(rect[3] - rect[1]));
    if (width <= 0 || height <= 0) continue;
    link.style.cssText = `left:${left}px;top:${top}px;width:${width}px;height:${height}px`;
    layer.append(link);
  }
  container.append(layer);
}
