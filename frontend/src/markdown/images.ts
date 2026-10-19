/** Sanitized Markdown image presentation, without mounted state or listeners.
 * Entry points: projectMarkdownImages, markdownImagePreviewSource.
 * Dependencies: shared image-source allowlist and Markdown environment ports.
 */
import { safeImageSource } from '../conversation/presentation/image-source-policy';
import type { MarkdownRenderEnvironment } from './ports';

/** Preview only the current source; model-authored metadata is not authority. */
export function markdownImagePreviewSource(image: HTMLImageElement): string {
  const source = image.getAttribute('src')?.trim() ?? '';
  if (!source || /^data:image\/svg\+xml/i.test(source)) return '';
  return safeImageSource(source) || (/^(?:api|static|uploads)\//i.test(source)
    ? safeImageSource(image.src) : '');
}

function imageOnly(node: Node): boolean {
  if (node.nodeType === 3) return !node.textContent?.trim();
  if (node.nodeType !== 1) return false;
  const element = node as Element;
  if (element.tagName === 'IMG' || element.tagName === 'BR') return true;
  return element.tagName === 'A' && Boolean(element.querySelector('img'))
    && Array.from(element.childNodes).every(imageOnly);
}

/** Called only after sanitization, local-file admission and proxy rewriting. */
export function projectMarkdownImages(html: string, environment: MarkdownRenderEnvironment): string {
  if (!environment.document || !/<img\b/i.test(html)) return html;
  const template = environment.document.createElement('template');
  template.innerHTML = html;
  for (const image of template.content.querySelectorAll<HTMLImageElement>('img')) {
    image.classList.add('md-image');
    image.classList.remove('md-preview-image');
    image.removeAttribute('data-preview-src');
    image.removeAttribute('data-conversation-action');
    image.removeAttribute('tabindex');
    image.removeAttribute('role');
    image.removeAttribute('aria-label');
    if (!image.closest('a, button') && markdownImagePreviewSource(image)) {
      image.classList.add('md-preview-image');
      image.setAttribute('role', 'button');
      image.tabIndex = 0;
      const label = environment.text?.('markdown.previewImage', 'Enlarge image') ?? 'Enlarge image';
      image.setAttribute('aria-label', image.alt ? `${label}: ${image.alt}` : label);
    }
  }
  for (const paragraph of template.content.querySelectorAll('p')) {
    paragraph.classList.remove('md-image-only');
    if (paragraph.querySelector('img') && Array.from(paragraph.childNodes).every(imageOnly)) {
      paragraph.classList.add('md-image-only');
    }
  }
  return template.innerHTML;
}
