/** Article figures, keyboard semantics and glossary popup geometry; no global listeners. */
export function frameFigures(article: HTMLElement): void {
  const document = article.ownerDocument;
  for (const image of article.querySelectorAll('img')) {
    if (image.closest('figure')) continue;
    const paragraph = image.closest('p');
    if (!paragraph || paragraph.querySelectorAll('img').length !== 1) continue;
    // Only image/caption paragraphs become figures; never discard surrounding
    // prose or another image merely because this image has alternative text.
    const remainder = paragraph.cloneNode(true) as HTMLElement;
    remainder.querySelector('img')?.remove();
    remainder.querySelector('em')?.remove();
    if (remainder.textContent?.trim() || remainder.children.length) continue;
    const figure = document.createElement('figure');
    figure.className = 'paper-figure';
    figure.append(image.cloneNode(true));
    const captionText = paragraph.querySelector('em')?.textContent?.trim() || image.alt.trim();
    if (captionText) {
      const caption = document.createElement('figcaption');
      caption.textContent = captionText;
      figure.append(caption);
    }
    paragraph.replaceWith(figure);
  }
}
export function decorateZoomableImages(root: HTMLElement, translate: (key: 'paper.imageZoomHint') => string): void {
  if (!root) return;
  for (const im of root.querySelectorAll('img')) {
    if (im.getAttribute('tabindex') === null) im.setAttribute('tabindex', '0');
    if (!im.getAttribute('role')) im.setAttribute('role', 'button');
    if (!im.getAttribute('aria-label')) {
      const alt = (im.getAttribute('alt') || '').trim();
      im.setAttribute('aria-label', (alt ? alt + ' — ' : '') +
        translate('paper.imageZoomHint'));
    }
  }
}
export function positionGlossaryCard(term: HTMLElement): void {
  if (!term) return;
  const card = term.querySelector<HTMLElement>(':scope > .paper-term-card');
  if (!card) return;
  const scroller = term.closest('.paper-report-content, .paper-report-body');
  if (!scroller) return;
  card.style.left = '';   // reset to the CSS default (0) before measuring
  const termRect = term.getBoundingClientRect();
  const scRect = scroller.getBoundingClientRect();
  const cardW = card.offsetWidth;
  const MARGIN = 8;
  // Default card-left in viewport coords == term's left edge.
  const minLeft = scRect.left + MARGIN;
  const maxLeft = scRect.right - MARGIN - cardW;
  let desired = termRect.left;
  if (maxLeft < minLeft) desired = minLeft;                 // card wider than column
  else desired = Math.min(Math.max(desired, minLeft), maxLeft);
  const offset = desired - termRect.left;
  if (offset) card.style.left = offset + 'px';
}
