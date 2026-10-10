/**
 * Retained HTML and detail disclosure primitives. Rich markup passes through
 * the existing reconciliation owner; no content policy.
 * Entry points: directClassChild, setManagedHtml, setStreamingMarkdownHtml, setCompletedMarkdownHtml, setManagedRichHtml,
 * resetManagedHtml, preserveDetailsOpen.
 * Dependencies: rich-card-reconciliation.
 */
import { reconcileRichCardHtml } from '../rich-card-reconciliation';

type ManagedHtmlElement = HTMLElement & {
  _tofuRenderedHtml?: string;
};

export function directClassChild<T extends HTMLElement>(
  parent: HTMLElement,
  className: string,
): T | null {
  for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
    if (child.classList.contains(className)) return child as T;
  }
  return null;
}

export function setManagedHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  node.innerHTML = html;
  managedNode._tofuRenderedHtml = html;
  return true;
}

function replaceChangedMarkdownSuffix(node: HTMLElement, html: string, previous?: string): boolean {
  let boundary = 0;
  let retained: Element | null = null;
  if (previous) {
    // Compute the shared boundary once; comparing every growing prefix would
    // make completion quadratic in the number of already-rendered blocks.
    let unchangedLength = 0;
    while (unchangedLength < previous.length && unchangedLength < html.length
        && previous.charCodeAt(unchangedLength) === html.charCodeAt(unchangedLength)) {
      unchangedLength++;
    }
    for (const child of Array.from(node.children)) {
      const serialized = child.outerHTML;
      const start = previous.indexOf(serialized, boundary);
      const end = start + serialized.length;
      if (start < 0 || previous.slice(boundary, start).trim()
          || end > unchangedLength) break;
      retained = child;
      boundary = end;
    }
  }
  if (!retained) return setManagedHtml(node, html);
  // Only complete parser/sanitizer output authorizes this replacement. A
  // changed multi-block tail must not destroy an identical reading prefix.
  const template = node.ownerDocument.createElement('template');
  template.innerHTML = html.slice(boundary);
  while (retained.nextSibling) node.removeChild(retained.nextSibling);
  node.appendChild(template.content);
  (node as ManagedHtmlElement)._tofuRenderedHtml = html;
  return true;
}

/** Preserve authorized unchanged reader blocks when terminal formatting arrives. */
export function setCompletedMarkdownHtml(node: HTMLElement, html: string): boolean {
  const previous = (node as ManagedHtmlElement)._tofuRenderedHtml;
  if (previous === html) return false;
  return replaceChangedMarkdownSuffix(node, html, previous);
}

export function setStreamingMarkdownHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  const previous = managedNode._tofuRenderedHtml;
  if (previous === html) return false;
  const tail = node.lastElementChild;
  if (!previous || !tail
      || (tail.tagName !== 'P' && tail.tagName !== 'PRE')) {
    return replaceChangedMarkdownSuffix(node, html, previous);
  }
  // The parser/sanitizer still authorizes the COMPLETE new HTML. Preserve
  // earlier DOM only when its exact HTML prefix is unchanged and the old
  // final block has the same canonical serialization as the mounted block.
  const oldTail = tail.outerHTML;
  const boundary = previous.lastIndexOf(oldTail);
  if (boundary < 0 || previous.slice(boundary + oldTail.length).trim()
      || !html.startsWith(previous.slice(0, boundary))) {
    return replaceChangedMarkdownSuffix(node, html, previous);
  }
  for (let sibling = tail.nextSibling; sibling; sibling = sibling.nextSibling) {
    if (sibling.nodeType !== 3 || sibling.textContent?.trim()) return replaceChangedMarkdownSuffix(node, html, previous);
  }
  const template = node.ownerDocument.createElement('template');
  template.innerHTML = html.slice(boundary);
  const nextTail = template.content.firstChild;
  // Growing plain paragraphs preserve their Text node and native selection.
  // Rich paragraphs/code replace only the changed final block. No source,
  // token or DOM cache is added beyond the existing managed HTML witness.
  if (tail.tagName === 'P' && !tail.attributes.length
      && tail.childNodes.length === 1 && tail.firstChild?.nodeType === 3
      && nextTail?.nodeType === 1 && (nextTail as Element).tagName === 'P'
      && !(nextTail as Element).attributes.length
      && nextTail.childNodes.length === 1 && nextTail.firstChild?.nodeType === 3
      && nextTail.textContent!.startsWith(tail.textContent ?? '')) {
    (tail.firstChild as Text).appendData(nextTail.textContent!.slice(tail.textContent!.length));
    while (tail.nextSibling) node.removeChild(tail.nextSibling);
    template.content.removeChild(nextTail);
    node.appendChild(template.content);
  } else {
    while (tail.nextSibling) node.removeChild(tail.nextSibling);
    node.replaceChild(template.content, tail);
  }
  managedNode._tofuRenderedHtml = html;
  return true;
}

export function setManagedRichHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  reconcileRichCardHtml(node, html);
  managedNode._tofuRenderedHtml = html;
  return true;
}

export function resetManagedHtml(node: HTMLElement): void {
  delete (node as ManagedHtmlElement)._tofuRenderedHtml;
}

/* Unified disclosure-state semantic for the replaceChildren-style blocks:
 * a disclosure's open state is USER INTENT, and the block's default state
 * applies only when the disclosure is first created. Re-rendering a block —
 * for a sibling's arrival, an unrelated field change, or (for a growing
 * system-note-group) a new member that defeats the sameBlock short-circuit —
 * must preserve the disclosure the reader already set, never snap it back to
 * the default. Reads the live <details> (first descendant) before the block
 * rebuilds its node; the caller then sets the rebuilt disclosure's open to
 * the returned value. Returns `defaultOpen` when no prior disclosure exists
 * (first paint, or a renderer-mode switch). */
export function preserveDetailsOpen(
  node: HTMLElement,
  defaultOpen: boolean,
): boolean {
  return node.querySelector('details')?.open ?? defaultOpen;
}
